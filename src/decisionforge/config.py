"""Strict configuration validation; no executable configuration."""

import hashlib
import json
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from jsonschema import Draft202012Validator, SchemaError
from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RetryPolicy(StrictModel):
    max_attempts: int = Field(default=3, ge=1, le=10)
    base_delay_seconds: int = Field(default=5, ge=1, le=3600)
    max_delay_seconds: int = Field(default=300, ge=1, le=86400)


OPS = {"exists", "missing", "eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in"}


def validate_condition(condition: Any, depth: int = 0) -> None:
    if depth > 16 or not isinstance(condition, dict):
        raise ValueError("conditions must be objects nested at most 16 levels")
    if len(condition) == 1 and next(iter(condition)) in {"all", "any", "not"}:
        kind, children = next(iter(condition.items()))
        if kind == "not":
            validate_condition(children, depth + 1)
        else:
            if not isinstance(children, list) or not children or len(children) > 50:
                raise ValueError("all/any require 1..50 conditions")
            for child in children:
                validate_condition(child, depth + 1)
        return
    op = condition.get("op")
    if not isinstance(op, str):
        raise ValueError("operator must be a string")
    expected_keys = {"field", "op"} if op in {"exists", "missing"} else {"field", "op", "value"}
    if set(condition) != expected_keys or op not in OPS:
        raise ValueError("invalid predicate shape or operator")
    field = condition["field"]
    if not isinstance(field, str) or not field.startswith(("input.", "dependencies.")):
        raise ValueError("field must reference input.* or dependencies.*")
    if any(not part or not part.replace("_", "a").isalnum() for part in field.split(".")):
        raise ValueError("field paths must contain simple dot-separated object keys")
    value = condition.get("value")
    if op in {"gt", "gte", "lt", "lte"} and type(value) not in {int, float}:
        raise ValueError("ordered comparisons require a numeric value")
    if op in {"in", "not_in"} and not isinstance(value, list):
        raise ValueError("membership comparisons require a list")
    canonical(condition)


class Rule(StrictModel):
    id: Identifier
    description: str = Field(min_length=1, max_length=500)
    condition: dict[str, Any]
    on_match: Identifier

    @model_validator(mode="after")
    def check_condition(self):
        validate_condition(self.condition)
        return self


class RulesStage(StrictModel):
    type: Literal["rules"]
    rules: list[Rule] = Field(min_length=1, max_length=100)
    default: Identifier


class DependencyStage(StrictModel):
    type: Literal["dependency"]
    adapter: Literal["simulated_verification"]
    next: Identifier
    on_failure: Identifier


class ManualStage(StrictModel):
    type: Literal["manual"]
    reason: str = Field(min_length=1, max_length=500)
    decisions: dict[Identifier, Identifier] = Field(min_length=1, max_length=10)


class TerminalStage(StrictModel):
    type: Literal["terminal"]
    outcome: Literal["APPROVED", "REJECTED"]
    reason: str = Field(min_length=1, max_length=500)


Stage = Annotated[
    RulesStage | DependencyStage | ManualStage | TerminalStage, Field(discriminator="type")
]


def targets(stage: Stage) -> list[str]:
    if isinstance(stage, RulesStage):
        return [stage.default, *(rule.on_match for rule in stage.rules)]
    if isinstance(stage, DependencyStage):
        return [stage.next, stage.on_failure]
    if isinstance(stage, ManualStage):
        return list(stage.decisions.values())
    return []


class Workflow(StrictModel):
    workflow_id: Identifier
    version: int = Field(ge=1)
    description: str = Field(min_length=1, max_length=1000)
    input_schema: dict[str, Any]
    start: Identifier
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    stages: dict[Identifier, Stage] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def check_graph(self):
        try:
            Draft202012Validator.check_schema(self.input_schema)
        except SchemaError as exc:
            raise ValueError("invalid input JSON Schema") from exc

        def check_refs(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in {"$ref", "$dynamicRef"}:
                        raise ValueError("schema references are unsupported; use inline schemas")
                    check_refs(value)
            elif isinstance(node, list):
                for child in node:
                    check_refs(child)

        check_refs(self.input_schema)
        if self.input_schema.get("type") != "object":
            raise ValueError("input_schema must declare an object")
        if self.start not in self.stages:
            raise ValueError("start stage does not exist")
        if not any(isinstance(s, TerminalStage) for s in self.stages.values()):
            raise ValueError("workflow needs a terminal stage")
        for name, stage in self.stages.items():
            if any(target not in self.stages for target in targets(stage)):
                raise ValueError(f"unknown transition target in {name}")
            if isinstance(stage, RulesStage):
                ids = [rule.id for rule in stage.rules]
                if len(ids) != len(set(ids)):
                    raise ValueError(f"duplicate rule IDs in {name}")
        visited, visiting = set(), set()

        def visit(name):
            if name in visiting:
                raise ValueError("workflow cycles are not supported")
            if name in visited:
                return
            visiting.add(name)
            for target in targets(self.stages[name]):
                visit(target)
            visiting.remove(name)
            visited.add(name)

        visit(self.start)
        if visited != set(self.stages):
            raise ValueError("all stages must be reachable from start")
        canonical(self.model_dump())
        return self


def load_workflow(path: str | Path) -> Workflow:
    return Workflow.model_validate(yaml.safe_load(Path(path).read_text()))
