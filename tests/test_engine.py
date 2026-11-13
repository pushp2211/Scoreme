import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from decisionforge.config import Workflow
from decisionforge.engine import Engine
from decisionforge.errors import DomainError
from decisionforge.storage import Store


def submit_run(engine, payload, key="key-1", workflow="loan_approval"):
    request, replay = engine.submit(workflow, payload, key)
    assert not replay
    return engine.run(request["id"])


def test_happy_path_and_complete_history(engine, loan):
    result = submit_run(engine, loan)
    assert result["state"] == "APPROVED"
    explanation = engine.explain(result["id"])
    assert explanation["events"][0]["data"]["input"] == loan
    assert explanation["config_hash"] == explanation["events"][0]["data"]["config_hash"]
    assert len(explanation["rule_trace"]) == 6
    assert [e["sequence"] for e in explanation["events"]] == list(
        range(1, len(explanation["events"]) + 1)
    )
    assert explanation["events"][-1]["data"]["to"]["state"] == "APPROVED"


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        ({"age": 17}, "underage"),
        ({"income": 29999}, "income_below_minimum"),
        ({"consent": False}, "missing_consent"),
        ({"simulation": {"verified": False}}, "identity_failed"),
        ({"simulation": {"risk_score": 80}}, "high_risk"),
    ],
)
def test_reject_with_explanation(engine, loan, change, rule):
    result = submit_run(engine, loan | change)
    assert result["state"] == "REJECTED"
    trace = engine.explain(result["id"])["rule_trace"]
    assert trace[-1]["data"]["rule_id"] == rule
    assert trace[-1]["data"]["matched"] is True


@pytest.mark.parametrize("payload", [{}, {"age": "30"}, {"surprise": 1}])
def test_invalid_input_has_no_side_effects(engine, store, loan, payload):
    with pytest.raises(DomainError) as error:
        engine.submit("loan_approval", payload if not payload else loan | payload, "invalid")
    assert error.value.status == 422
    with store.transaction(write=False) as connection:
        assert connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_duplicate_replay_and_conflict(engine, loan):
    original = submit_run(engine, loan)
    replay, is_replay = engine.submit("loan_approval", dict(reversed(list(loan.items()))), "key-1")
    assert is_replay and replay["id"] == original["id"]
    event_count = len(engine.explain(original["id"])["events"])
    assert engine.run(original["id"]) == original
    assert len(engine.explain(original["id"])["events"]) == event_count
    with pytest.raises(DomainError) as error:
        engine.submit("loan_approval", loan | {"amount": 50000}, "key-1")
    assert error.value.status == 409


def test_concurrent_duplicates_and_workers(engine, store, clock, loan):
    def intake(_):
        other = Engine(store, clock)
        request, _ = other.submit("loan_approval", loan, "concurrent")
        return other.run(request["id"])

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(intake, range(16)))
    assert len({r["id"] for r in results}) == 1
    assert all(r["state"] == "APPROVED" for r in results)
    events = engine.explain(results[0]["id"])["events"]
    assert sum(e["kind"] == "dependency_succeeded" for e in events) == 1
    assert sum(e["kind"] == "submitted" for e in events) == 1


def test_retry_is_due_persistent_and_successful(engine, store, clock, loan):
    result = submit_run(engine, loan | {"simulation": {"fail_until": 1}})
    assert result["state"] == "RETRY_PENDING"
    assert result["next_attempt_at"] == clock() + 2
    assert result["attempts"] == {"identity": 1}
    assert engine.work_once() == 0
    assert engine.run(result["id"]) == result
    restarted = Engine(Store(store.path), clock)
    clock.advance(2)
    assert restarted.work_once() == 1
    result = restarted.get(result["id"])
    assert result["state"] == "APPROVED"
    assert result["attempts"] == {"identity": 2}
    assert result["next_attempt_at"] is None
    traces = restarted.explain(result["id"])["rule_trace"]
    assert sum(e["data"]["rule_id"] == "underage" for e in traces) == 1


def test_exhaustion_and_manual_resolution(engine, clock, loan):
    result = submit_run(engine, loan | {"simulation": {"fail_until": 10}})
    clock.advance(2)
    result = engine.run(result["id"])
    assert result["next_attempt_at"] == clock() + 4
    clock.advance(4)
    result = engine.run(result["id"])
    assert result["state"] == "MANUAL_REVIEW"
    assert result["attempts"] == {"identity": 3}
    result = engine.resolve(result["id"], "reject", "reviewer-42", "Verification unavailable")
    assert result["state"] == "REJECTED"
    resolution = [
        e for e in engine.explain(result["id"])["events"] if e["kind"] == "manual_resolved"
    ]
    assert resolution[0]["data"]["actor"] == "reviewer-42"
    with pytest.raises(DomainError) as error:
        engine.resolve(result["id"], "approve", "reviewer-42", "Second review")
    assert error.value.status == 409


def test_permanent_error_skips_retries(engine, loan):
    result = submit_run(engine, loan | {"simulation": {"permanent_failure": True}})
    assert result["state"] == "MANUAL_REVIEW"
    assert result["attempts"]["identity"] == 1
    assert not any(e["kind"] == "retry_scheduled" for e in engine.explain(result["id"])["events"])


def test_manual_branch_and_invalid_decision(engine, loan):
    result = submit_run(engine, loan | {"amount": 100001})
    assert result["state"] == "MANUAL_REVIEW"
    with pytest.raises(DomainError) as error:
        engine.resolve(result["id"], "defer", "reviewer", "Need more data")
    assert error.value.status == 422
    assert (
        engine.resolve(result["id"], "approve", "reviewer", "Reviewed income")["state"]
        == "APPROVED"
    )


def test_atomic_partial_save_failure_and_recovery(engine, store, loan, monkeypatch):
    queued, _ = engine.submit("loan_approval", loan, "crash")
    original = store.append_event

    def broken_event(connection, request_id, now, kind, data):
        # transition() has already saved state: now emulate disk failure writing the audit.
        if kind == "transition" and data["to"]["state"] == "APPROVED":
            raise sqlite3.OperationalError("injected partial save failure")
        return original(connection, request_id, now, kind, data)

    monkeypatch.setattr(store, "append_event", broken_event)
    with pytest.raises(sqlite3.OperationalError):
        engine.run(queued["id"])
    restored = engine.explain(queued["id"])
    assert restored["request"] == queued
    assert len(restored["events"]) == 1
    monkeypatch.setattr(store, "append_event", original)
    assert engine.work_once() == 1
    assert engine.get(queued["id"])["state"] == "APPROVED"


def test_crash_between_intake_and_execution_recovered(store, clock, loan):
    original = Engine(store, clock)
    request, _ = original.submit("loan_approval", loan, "queued")
    assert request["state"] == "QUEUED"
    restarted = Engine(Store(store.path), clock)
    assert restarted.work_once() == 1
    assert restarted.get(request["id"])["state"] == "APPROVED"


def test_pinned_rules_and_schema_across_change(engine, store, clock, loan):
    old = submit_run(engine, loan | {"simulation": {"fail_until": 1}}, "old")
    with store.transaction(write=False) as connection:
        config = store.workflow(connection, "loan_approval").model_dump()
    config["version"] = 2
    config["stages"]["risk"]["rules"][1]["condition"]["value"] = 5
    config["input_schema"]["required"].append("new_document")
    config["input_schema"]["properties"]["new_document"] = {"type": "boolean"}
    store.publish(Workflow.model_validate(config), clock())
    clock.advance(2)
    assert engine.run(old["id"])["state"] == "APPROVED"
    new = submit_run(engine, loan | {"new_document": True}, "new")
    assert new["state"] == "REJECTED" and new["workflow_version"] == 2
    replay, is_replay = engine.submit("loan_approval", old["payload"], "old")
    assert is_replay and replay["workflow_version"] == 1
    with pytest.raises(DomainError):
        engine.submit("loan_approval", loan, "invalid-v2")


def test_versions_immutable_and_monotonic(store, clock):
    with store.transaction(write=False) as connection:
        config = store.workflow(connection, "loan_approval").model_dump()
    assert store.publish(Workflow.model_validate(config), clock()) is False
    config["description"] = "Changed"
    with pytest.raises(DomainError):
        store.publish(Workflow.model_validate(config), clock())
    config["version"] = 3
    store.publish(Workflow.model_validate(config), clock())
    config["version"] = 2
    with pytest.raises(DomainError):
        store.publish(Workflow.model_validate(config), clock())


@pytest.mark.parametrize(
    ("table", "verb"),
    [("events", "DELETE"), ("events", "UPDATE"), ("workflows", "DELETE"), ("workflows", "UPDATE")],
)
def test_database_immutability(engine, store, loan, table, verb):
    submit_run(engine, loan)
    statement = (
        f"DELETE FROM {table}"
        if verb == "DELETE"
        else (f"UPDATE {table} SET " + ("kind=kind" if table == "events" else "config=config"))
    )
    with pytest.raises(sqlite3.IntegrityError), store.transaction() as connection:
        connection.execute(statement)


def test_vendor_uses_same_engine(engine):
    payload = {"vendor_id": "vendor-1", "country": "IN", "documents": True, "annual_spend": 10000}
    assert submit_run(engine, payload, "vendor-1", "vendor_onboarding")["state"] == "APPROVED"
    assert (
        submit_run(engine, payload | {"documents": False}, "vendor-2", "vendor_onboarding")["state"]
        == "REJECTED"
    )
    assert (
        submit_run(engine, payload | {"country": "OTHER"}, "vendor-3", "vendor_onboarding")["state"]
        == "MANUAL_REVIEW"
    )


def test_unknown_resources(engine, loan):
    with pytest.raises(DomainError) as error:
        engine.get("absent")
    assert error.value.status == 404
    with pytest.raises(DomainError):
        engine.submit("absent", loan, "key")
