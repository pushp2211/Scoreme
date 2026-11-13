import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from decisionforge.config import Workflow, load_workflow
from decisionforge.engine import Engine
from decisionforge.errors import DomainError
from decisionforge.rules import evaluate
from decisionforge.storage import Store


def config_of(store):
    with store.transaction(write=False) as connection:
        return store.workflow(connection, "loan_approval").model_dump()


@pytest.mark.parametrize(
    "condition",
    [
        {"field": "input..age", "op": "gt", "value": 18},
        {"field": "os.environ", "op": "exists"},
        {"field": "input.age", "op": ["gt"], "value": 18},
        {"field": "input.age", "op": "in", "value": "123"},
        {"any": []},
        {"not": "bad"},
    ],
)
def test_reject_malformed_predicates(store, condition):
    config = config_of(store)
    config["stages"]["eligibility"]["rules"][0]["condition"] = condition
    with pytest.raises(ValidationError):
        Workflow.model_validate(config)


def test_depth_limit_and_schema_refs(store):
    config = config_of(store)
    nested = {"field": "input.age", "op": "exists"}
    for _ in range(18):
        nested = {"not": nested}
    config["stages"]["eligibility"]["rules"][0]["condition"] = nested
    with pytest.raises(ValidationError):
        Workflow.model_validate(config)
    config = config_of(store)
    config["input_schema"]["properties"]["age"] = {"$ref": "#/missing"}
    with pytest.raises(ValidationError):
        Workflow.model_validate(config)


@pytest.mark.parametrize("change", ["start", "terminal", "schema", "membership"])
def test_invalid_structure(store, change):
    config = config_of(store)
    if change == "start":
        config["start"] = "absent"
    elif change == "terminal":
        for name in ["approved", "rejected"]:
            config["stages"][name] = {"type": "manual", "reason": "Loop", "decisions": {"x": name}}
    elif change == "schema":
        config["input_schema"] = {"type": "array"}
    elif change == "membership":
        config["stages"]["risk"]["rules"][1]["condition"] = {"all": "invalid"}
    with pytest.raises(ValidationError):
        Workflow.model_validate(config)


def test_recursive_equality_is_typed():
    for actual, expected, matched in [
        ({"x": True}, {"x": 1}, False),
        ([True], [1], False),
        ({"x": [1, 2]}, {"x": [1.0, 2]}, True),
        ([1], [1, 2], False),
    ]:
        result, _ = evaluate(
            {"field": "input.x", "op": "eq", "value": expected}, {"input": {"x": actual}}
        )
        assert result is matched


def test_reserved_simulation_validation(store, engine, loan, clock):
    config = config_of(store)
    config["workflow_id"] = "free_form"
    config["input_schema"] = {"type": "object"}
    store.publish(Workflow.model_validate(config), clock())
    with pytest.raises(DomainError) as error:
        engine.submit("free_form", loan | {"simulation": {"fail_until": "invalid"}}, "bad-sim")
    assert error.value.code == "invalid_simulation"


def test_retry_backoff_cap_and_multiple_dependency_stages(store, engine, clock, loan):
    config = config_of(store)
    config["workflow_id"] = "multi_verification"
    config["retry"]["max_delay_seconds"] = 3
    config["retry"]["max_attempts"] = 4
    config["stages"]["identity"]["next"] = "second_verification"
    config["stages"]["second_verification"] = {
        "type": "dependency",
        "adapter": "simulated_verification",
        "next": "risk",
        "on_failure": "review",
    }
    store.publish(Workflow.model_validate(config), clock())
    request, _ = engine.submit(
        "multi_verification", loan | {"simulation": {"fail_until": 2}}, "multi"
    )
    result = engine.run(request["id"])
    assert result["next_attempt_at"] == clock() + 2
    clock.advance(2)
    result = engine.run(request["id"])
    assert result["next_attempt_at"] == clock() + 3
    clock.advance(3)
    result = engine.run(request["id"])
    assert result["stage"] == "second_verification"
    assert result["attempts"] == {"identity": 3, "second_verification": 1}
    clock.advance(2)
    engine.run(request["id"])
    clock.advance(3)
    result = engine.run(request["id"])
    assert result["state"] == "APPROVED"
    assert result["attempts"] == {"identity": 3, "second_verification": 3}


def test_failed_manual_save_rolls_back_resolution(store, engine, loan, monkeypatch):
    request, _ = engine.submit("loan_approval", loan | {"amount": 200000}, "manual-rollback")
    before = engine.run(request["id"])
    events_before = engine.explain(request["id"])["events"]
    original = store.append_event

    def fail_final(connection, request_id, now, kind, data):
        if kind == "transition" and data["to"]["state"] == "APPROVED":
            raise sqlite3.OperationalError("injected audit disk failure")
        original(connection, request_id, now, kind, data)

    monkeypatch.setattr(store, "append_event", fail_final)
    with pytest.raises(sqlite3.OperationalError):
        engine.resolve(request["id"], "approve", "reviewer", "Verified")
    assert engine.get(request["id"]) == before
    assert engine.explain(request["id"])["events"] == events_before


def test_worker_isolates_one_failed_request(store, engine, loan, monkeypatch):
    first, _ = engine.submit("loan_approval", loan, "broken")
    second, _ = engine.submit("loan_approval", loan, "working")
    original = engine.run

    def fail_one(request_id):
        if request_id == first["id"]:
            raise RuntimeError("unexpected adapter failure")
        return original(request_id)

    monkeypatch.setattr(engine, "run", fail_one)
    assert engine.work_once() == 2
    assert engine.get(first["id"])["state"] == "QUEUED"
    assert engine.get(second["id"])["state"] == "APPROVED"
    monkeypatch.setattr(engine, "run", original)
    assert engine.work_once() == 1


def test_concurrent_manual_resolution(store, clock, loan, engine):
    request, _ = engine.submit("loan_approval", loan | {"amount": 200000}, "concurrent-manual")
    engine.run(request["id"])

    def resolve(_):
        try:
            return Engine(store, clock).resolve(request["id"], "approve", "reviewer", "Checked")[
                "state"
            ]
        except DomainError as error:
            return error.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(resolve, range(2)))
    assert set(results) == {"APPROVED", 409}
    assert sum(e["kind"] == "manual_resolved" for e in engine.explain(request["id"])["events"]) == 1


def test_filesystem_database_required():
    with pytest.raises(ValueError):
        Store(":memory:")


def test_loader_rejects_executable_yaml(tmp_path):
    path = tmp_path / "unsafe.yaml"
    path.write_text('!!python/object/apply:os.system ["echo unsafe"]')
    import yaml

    with pytest.raises(yaml.YAMLError):
        load_workflow(path)
