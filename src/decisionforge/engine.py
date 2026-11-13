"""Durable, configuration-driven workflow orchestration."""

import time
import uuid
from collections.abc import Callable

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from decisionforge.config import (
    DependencyStage,
    ManualStage,
    RulesStage,
    TerminalStage,
    canonical,
    digest,
)
from decisionforge.dependencies import DependencyFailure, Simulation, verify
from decisionforge.errors import DomainError
from decisionforge.rules import evaluate
from decisionforge.storage import Store


class Engine:
    def __init__(self, store: Store, clock: Callable[[], float] = time.time):
        self.store, self.clock = store, clock

    def submit(self, workflow_id: str, payload: dict, key: str) -> tuple[dict, bool]:
        input_hash = digest({"workflow_id": workflow_id, "payload": payload})
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT id,input_hash FROM requests WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing:
                if existing["input_hash"] != input_hash:
                    raise DomainError(
                        "idempotency_conflict", "Key was used with different input", 409
                    )
                return self.store.request(connection, existing["id"]), True
            workflow = self.store.workflow(connection, workflow_id)
            validator = Draft202012Validator(workflow.input_schema)
            errors = list(validator.iter_errors(payload))
            if errors:
                details = [
                    {
                        "path": "/" + "/".join(map(str, error.absolute_path)),
                        "message": error.message,
                    }
                    for error in errors[:20]
                ]
                raise DomainError(
                    "invalid_payload", "Input does not match workflow schema", 422, details
                )
            # Simulation is reserved even when a custom schema accepts arbitrary input.
            try:
                Simulation.model_validate(payload.get("simulation", {}))
            except ValidationError as exc:
                raise DomainError(
                    "invalid_simulation", "Invalid dependency simulation", 422
                ) from exc
            request_id, now = str(uuid.uuid4()), self.clock()
            connection.execute(
                "INSERT INTO requests (id,idempotency_key,input_hash,workflow_id,workflow_version,"
                "state,stage,payload,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    key,
                    input_hash,
                    workflow_id,
                    workflow.version,
                    "QUEUED",
                    workflow.start,
                    canonical(payload),
                    now,
                    now,
                ),
            )
            self.store.append_event(
                connection,
                request_id,
                now,
                "submitted",
                {
                    "state": "QUEUED",
                    "stage": workflow.start,
                    "workflow_id": workflow_id,
                    "workflow_version": workflow.version,
                    "config_hash": digest(workflow.model_dump()),
                    "input_hash": input_hash,
                    "input": payload,
                },
            )
            return self.store.request(connection, request_id), False

    def get(self, request_id: str) -> dict:
        with self.store.transaction(write=False) as connection:
            return self.store.request(connection, request_id)

    def transition(self, connection, request: dict, state: str, stage: str, reason: str):
        before = {"state": request["state"], "stage": request["stage"]}
        request.update(state=state, stage=stage, updated_at=self.clock())
        self.store.save(connection, request)
        self.store.append_event(
            connection,
            request["id"],
            self.clock(),
            "transition",
            {
                "from": before,
                "to": {"state": state, "stage": stage},
                "reason": reason,
            },
        )

    def run(self, request_id: str) -> dict:
        with self.store.transaction() as connection:
            request = self.store.request(connection, request_id)
            if request["state"] not in {"QUEUED", "RETRY_PENDING"}:
                return request
            if request["next_attempt_at"] is not None and request["next_attempt_at"] > self.clock():
                return request
            self._execute(connection, request)
            return request

    def _execute(self, connection, request: dict):
        workflow = self.store.workflow(
            connection, request["workflow_id"], request["workflow_version"]
        )
        request["next_attempt_at"] = None
        self.transition(connection, request, "RUNNING", request["stage"], "execution_started")
        for _ in range(len(workflow.stages) + 1):
            name = request["stage"]
            stage = workflow.stages[name]
            if isinstance(stage, TerminalStage):
                self.transition(connection, request, stage.outcome, name, stage.reason)
                return
            if isinstance(stage, ManualStage):
                self.transition(connection, request, "MANUAL_REVIEW", name, stage.reason)
                return
            if isinstance(stage, RulesStage):
                target, reason = stage.default, "no_rule_matched"
                for rule in stage.rules:
                    matched, trace = evaluate(
                        rule.condition,
                        {
                            "input": request["payload"],
                            "dependencies": request["dependencies"],
                        },
                    )
                    self.store.append_event(
                        connection,
                        request["id"],
                        self.clock(),
                        "rule_evaluated",
                        {
                            "stage": name,
                            "rule_id": rule.id,
                            "description": rule.description,
                            "matched": matched,
                            "condition": trace,
                            "on_match": rule.on_match,
                        },
                    )
                    if matched:
                        target, reason = rule.on_match, f"rule:{rule.id}"
                        break
                self.transition(connection, request, "RUNNING", target, reason)
            elif isinstance(stage, DependencyStage):
                attempt = request["attempts"].get(name, 0) + 1
                request["attempts"][name] = attempt
                operation_key = f"{request['id']}:{name}"
                try:
                    result = verify(request["payload"], attempt, operation_key)
                except DependencyFailure as failure:
                    self.store.append_event(
                        connection,
                        request["id"],
                        self.clock(),
                        "dependency_failed",
                        {
                            "stage": name,
                            "adapter": stage.adapter,
                            "attempt": attempt,
                            "code": failure.code,
                            "transient": failure.transient,
                            "operation_key": operation_key,
                        },
                    )
                    if failure.transient and attempt < workflow.retry.max_attempts:
                        delay = min(
                            workflow.retry.base_delay_seconds * 2 ** (attempt - 1),
                            workflow.retry.max_delay_seconds,
                        )
                        request["next_attempt_at"] = self.clock() + delay
                        self.transition(
                            connection, request, "RETRY_PENDING", name, "transient_failure"
                        )
                        self.store.append_event(
                            connection,
                            request["id"],
                            self.clock(),
                            "retry_scheduled",
                            {
                                "stage": name,
                                "attempt": attempt,
                                "delay_seconds": delay,
                                "next_attempt_at": request["next_attempt_at"],
                            },
                        )
                        return
                    reason = (
                        "retry_exhausted" if failure.transient else "permanent_dependency_failure"
                    )
                    self.transition(connection, request, "RUNNING", stage.on_failure, reason)
                else:
                    request["dependencies"][name] = result
                    self.store.append_event(
                        connection,
                        request["id"],
                        self.clock(),
                        "dependency_succeeded",
                        {
                            "stage": name,
                            "adapter": stage.adapter,
                            "attempt": attempt,
                            "result": result,
                        },
                    )
                    self.transition(
                        connection, request, "RUNNING", stage.next, "dependency_succeeded"
                    )
        raise RuntimeError("Workflow exceeded its validated execution bound")

    def resolve(self, request_id: str, decision: str, actor: str, reason: str) -> dict:
        with self.store.transaction() as connection:
            request = self.store.request(connection, request_id)
            if request["state"] != "MANUAL_REVIEW":
                raise DomainError("invalid_state", "Request is not awaiting manual review", 409)
            workflow = self.store.workflow(
                connection, request["workflow_id"], request["workflow_version"]
            )
            stage = workflow.stages[request["stage"]]
            if not isinstance(stage, ManualStage) or decision not in stage.decisions:
                raise DomainError("invalid_decision", "Decision is not allowed at this stage", 422)
            self.store.append_event(
                connection,
                request_id,
                self.clock(),
                "manual_resolved",
                {
                    "stage": request["stage"],
                    "actor": actor,
                    "reason": reason,
                    "decision": decision,
                },
            )
            self.transition(
                connection, request, "RUNNING", stage.decisions[decision], "manual_resolution"
            )
            self._execute(connection, request)
            return request

    def work_once(self, limit: int = 100) -> int:
        with self.store.transaction(write=False) as connection:
            rows = connection.execute(
                "SELECT id FROM requests WHERE state='QUEUED' OR "
                "(state='RETRY_PENDING' AND next_attempt_at<=?) ORDER BY created_at,id LIMIT ?",
                (self.clock(), limit),
            ).fetchall()
        for row in rows:
            self.run(row["id"])
        return len(rows)

    def explain(self, request_id: str) -> dict:
        # A read transaction gives the response and its audit one consistent snapshot.
        with self.store.transaction(write=False) as connection:
            request = self.store.request(connection, request_id)
            workflow = self.store.workflow(
                connection, request["workflow_id"], request["workflow_version"]
            )
            events = self.store.events(connection, request_id)
            return {
                "request": request,
                "config_hash": digest(workflow.model_dump()),
                "workflow": workflow.model_dump(),
                "rule_trace": [e for e in events if e["kind"] == "rule_evaluated"],
                "events": events,
            }
