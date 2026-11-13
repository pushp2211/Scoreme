from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from decisionforge.config import Workflow, load_workflow
from decisionforge.rules import evaluate

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("file", sorted((ROOT / "configs").glob("*.yaml")))
def test_examples_validate(file):
    assert load_workflow(file).version == 1


@pytest.mark.parametrize(
    ("op", "actual", "expected", "result"),
    [
        ("eq", True, 1, False),
        ("ne", True, 1, True),
        ("gte", 18, 18, True),
        ("gt", 18, 18, False),
        ("lt", 17, 18, True),
        ("lte", 18, 18, True),
        ("gte", "18", 18, False),
        ("gte", True, 1, False),
        ("in", "IN", ["IN", "US"], True),
        ("not_in", "GB", ["IN"], True),
    ],
)
def test_typed_predicates(op, actual, expected, result):
    matched, trace = evaluate(
        {"field": "input.x", "op": op, "value": expected}, {"input": {"x": actual}}
    )
    assert matched is result
    assert trace["actual"] == actual


@pytest.mark.parametrize("op", ["eq", "ne", "gt", "in", "not_in"])
def test_missing_comparisons_never_match(op):
    result, trace = evaluate({"field": "input.x", "op": op, "value": []}, {"input": {}})
    assert result is False
    assert trace["present"] is False


def test_nested_complete_trace():
    condition = {
        "all": [
            {"field": "input.x", "op": "missing"},
            {"not": {"field": "input.y", "op": "exists"}},
            {
                "any": [
                    {"field": "input.z", "op": "eq", "value": 1},
                    {"field": "input.z", "op": "eq", "value": 2},
                ]
            },
        ]
    }
    result, trace = evaluate(condition, {"input": {"z": 1}})
    assert result is True
    assert len(trace["children"][2]["children"]) == 2


@pytest.fixture
def config():
    return load_workflow(ROOT / "configs/loan_approval.v1.yaml").model_dump()


@pytest.mark.parametrize(
    "change",
    [
        "cycle",
        "target",
        "unreachable",
        "operator",
        "remote_ref",
        "duplicate_rule",
        "unknown_key",
        "numeric",
        "empty_group",
    ],
)
def test_invalid_configuration(config, change):
    bad = deepcopy(config)
    rule = bad["stages"]["eligibility"]["rules"][0]
    if change == "cycle":
        bad["stages"]["eligibility"]["default"] = "eligibility"
    elif change == "target":
        rule["on_match"] = "absent"
    elif change == "unreachable":
        bad["stages"]["unused"] = {"type": "terminal", "outcome": "REJECTED", "reason": "Unused"}
    elif change == "operator":
        rule["condition"]["op"] = "eval"
    elif change == "remote_ref":
        bad["input_schema"]["properties"]["age"] = {"$ref": "https://example.com/schema"}
    elif change == "duplicate_rule":
        bad["stages"]["eligibility"]["rules"].append(rule)
    elif change == "unknown_key":
        bad["unexpected"] = True
    elif change == "numeric":
        rule["condition"] = {"field": "input.age", "op": "gt", "value": "18"}
    elif change == "empty_group":
        rule["condition"] = {"all": []}
    with pytest.raises((ValueError, ValidationError)):
        Workflow.model_validate(bad)
