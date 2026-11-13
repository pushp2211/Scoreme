import sqlite3

import pytest
from fastapi.testclient import TestClient

from decisionforge.api import MAX_BODY, create_app


@pytest.fixture
def client(engine):
    with TestClient(create_app(engine=engine)) as client:
        yield client


def intake(client, loan, key="api-key"):
    return client.post(
        "/requests",
        json={"workflow_id": "loan_approval", "payload": loan},
        headers={"Idempotency-Key": key},
    )


def test_http_happy_replay_conflict_and_explanation(client, loan):
    response = intake(client, loan)
    assert response.status_code == 201
    result = response.json()
    assert result["state"] == "APPROVED"
    assert "idempotency_key" not in result
    assert response.headers["Location"] == f"/requests/{result['id']}"
    replay = intake(client, loan)
    assert replay.status_code == 200 and replay.json()["id"] == result["id"]
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert intake(client, loan | {"age": 45}).status_code == 409
    assert client.get(response.headers["Location"]).json()["state"] == "APPROVED"
    explanation = client.get(f"/requests/{result['id']}/explanation").json()
    assert explanation["rule_trace"]
    assert "idempotency_key" not in explanation["request"]
    assert client.get(f"/requests/{result['id']}/audit").json() == explanation["events"]
    assert len(client.get("/requests").json()) == 1
    assert client.get("/requests?limit=101").status_code == 422


def test_schema_and_header_errors(client, loan):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").json()["info"]["title"] == "DecisionForge"
    assert intake(client, loan | {"age": "30"}).status_code == 422
    assert client.post("/requests", json={}).status_code == 422
    for key in ["", "bad key", "x" * 129]:
        assert intake(client, loan, key).status_code == 422
    assert client.get("/requests/absent").status_code == 404
    assert client.get("/requests/absent/audit").status_code == 404
    assert client.get("/workflows/absent").status_code == 404


@pytest.mark.parametrize("content", ["{broken", '{"n":NaN}', '{"n":Infinity}', '{"n":1e999}'])
def test_invalid_json(client, content):
    response = client.post(
        "/requests", content=content, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_json"


def test_body_limit_streaming(client):
    response = client.post("/requests", content=iter([b" " * MAX_BODY, b"x"]))
    assert response.status_code == 413


def test_manual_api_and_retry(client, engine, clock, loan):
    pending = intake(client, loan | {"simulation": {"fail_until": 1}}).json()
    assert pending["state"] == "RETRY_PENDING"
    clock.advance(2)
    engine.work_once()
    assert client.get(f"/requests/{pending['id']}").json()["state"] == "APPROVED"
    manual = intake(client, loan | {"amount": 200000}, "manual").json()
    response = client.post(
        f"/requests/{manual['id']}/resolve",
        json={"decision": "approve", "actor": "reviewer", "reason": "Verified"},
    )
    assert response.status_code == 200 and response.json()["state"] == "APPROVED"
    assert client.post(f"/requests/{manual['id']}/resolve", json={}).status_code == 422


def test_publication_and_config_errors(client):
    assert len(client.get("/workflows").json()) == 2
    config = client.get("/workflows/loan_approval").json()
    assert client.post("/workflows", json=config).json()["created"] is False
    config["version"] = 2
    config["description"] = "Version two"
    assert client.post("/workflows", json=config).status_code == 201
    assert client.get("/workflows/loan_approval").json()["version"] == 2
    assert client.get("/workflows/loan_approval?version=1").json()["version"] == 1
    config["description"] = "Illegal mutation"
    assert client.post("/workflows", json=config).status_code == 409
    config["version"] = 3
    config["stages"]["identity"]["next"] = "absent"
    assert client.post("/workflows", json=config).status_code == 422
    config["input_schema"] = {"type": "INVALID"}
    assert client.post("/workflows", json=config).status_code == 422


def test_authentication_covers_reads_writes_and_docs(engine, loan):
    with TestClient(create_app(engine=engine, api_token="test-secret")) as secured:
        for path in ["/health", "/workflows", "/requests", "/docs", "/openapi.json"]:
            assert secured.get(path).status_code == 401
            assert (
                secured.get(path, headers={"Authorization": "Bearer test-secret"}).status_code
                == 200
            )
        assert intake(secured, loan).status_code == 401
        assert (
            secured.post(
                "/requests",
                json={"workflow_id": "loan_approval", "payload": loan},
                headers={"Authorization": "Bearer test-secret", "Idempotency-Key": "secured"},
            ).status_code
            == 201
        )


def test_api_reports_storage_failure_without_partial_commit(client, engine, loan, monkeypatch):
    original = engine.store.append_event

    def broken(connection, request_id, now, kind, data):
        if kind == "transition":
            raise sqlite3.OperationalError("injected")
        original(connection, request_id, now, kind, data)

    monkeypatch.setattr(engine.store, "append_event", broken)
    response = intake(client, loan)
    assert response.status_code == 503
    queued = client.get("/requests").json()[0]
    assert queued["state"] == "QUEUED"
    monkeypatch.setattr(engine.store, "append_event", original)
    replay = intake(client, loan)
    assert replay.status_code == 200 and replay.json()["state"] == "APPROVED"
