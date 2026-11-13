"""REST API factory. Run with `decisionforge serve`."""

import hmac
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import StringConstraints, ValidationError

from decisionforge.config import Identifier, StrictModel, Workflow, load_workflow
from decisionforge.engine import Engine
from decisionforge.errors import DomainError
from decisionforge.storage import Store

logger = logging.getLogger(__name__)
MAX_BODY = 1_048_576


class Intake(StrictModel):
    workflow_id: Identifier
    payload: dict[str, Any]


Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class Resolution(StrictModel):
    decision: Identifier
    actor: Text
    reason: Text


class RequestView(StrictModel):
    id: str
    input_hash: str
    workflow_id: str
    workflow_version: int
    state: str
    stage: str
    payload: dict[str, Any]
    dependencies: dict[str, Any]
    attempts: dict[str, int]
    next_attempt_at: float | None
    created_at: float
    updated_at: float


def public(request: dict) -> dict:
    return {key: value for key, value in request.items() if key != "idempotency_key"}


def error_response(status: int, code: str, message: str, details=None):
    return JSONResponse(
        status_code=status,
        content={
            "error": {"code": code, "message": message, "details": details},
        },
    )


def create_app(
    db_path: str | None = None,
    config_dir: str | None = None,
    api_token: str | None = None,
    engine: Engine | None = None,
) -> FastAPI:
    """Inject an engine in tests, or configure using DECISIONFORGE_* environment variables."""
    if engine is None:
        store = Store(db_path or os.getenv("DECISIONFORGE_DB", "data/decisionforge.db"))
        engine = Engine(store)
        directory = Path(config_dir or os.getenv("DECISIONFORGE_CONFIG_DIR", "configs"))
        if not directory.is_dir():
            raise ValueError(f"Configuration directory does not exist: {directory}")
        for path in sorted(directory.glob("*.yaml")):
            store.publish(load_workflow(path), engine.clock())
    token = api_token if api_token is not None else os.getenv("DECISIONFORGE_API_TOKEN")

    def authenticate(authorization: Annotated[str | None, Header()] = None):
        if token and not hmac.compare_digest(authorization or "", f"Bearer {token}"):
            raise DomainError("unauthorized", "Valid bearer authentication is required", 401)

    app = FastAPI(
        title="DecisionForge",
        version="1.0.0",
        description="Configurable, durable and explainable workflow decisions. "
        "Idempotency-Key is required on intake; replay returns the existing resource.",
        dependencies=[Depends(authenticate)],
    )
    app.state.engine = engine

    @app.middleware("http")
    async def validate_body(request: Request, call_next):
        # Read incrementally to enforce the limit even without Content-Length.
        if request.method in {"POST", "PUT", "PATCH"}:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_BODY:
                    return error_response(413, "body_too_large", "Request body exceeds 1 MiB")
            if body:
                try:

                    def invalid_constant(value):
                        raise ValueError(f"Non-finite JSON number: {value}")

                    parsed = json.loads(body, parse_constant=invalid_constant)
                    # Also rejects overflowing floats such as 1e999.
                    json.dumps(parsed, allow_nan=False)
                except (ValueError, UnicodeDecodeError):
                    return error_response(
                        422, "invalid_json", "Body must contain finite, valid JSON"
                    )
            request._body = bytes(body)
        # Swagger and OpenAPI are useful, but schema/docs are protected if auth is enabled.
        if token and request.url.path in {"/docs", "/redoc", "/openapi.json"}:
            if not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {token}"):
                return error_response(
                    401, "unauthorized", "Valid bearer authentication is required"
                )
        return await call_next(request)

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        return error_response(exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        details = [{"path": list(error["loc"]), "message": error["msg"]} for error in exc.errors()]
        return error_response(422, "invalid_request", "Request validation failed", details)

    @app.exception_handler(ValidationError)
    async def invalid_config(request: Request, exc: ValidationError):
        return error_response(422, "invalid_configuration", "Configuration validation failed")

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request: Request, exc: sqlite3.Error):
        logger.exception("Storage operation failed")
        return error_response(
            503,
            "storage_unavailable",
            "Storage operation failed; retry with the same idempotency key",
        )

    @app.get("/health", tags=["operations"])
    def health():
        with engine.store.transaction(write=False) as connection:
            connection.execute("SELECT 1").fetchone()
        return {"status": "ok", "service": "decisionforge"}

    @app.post("/workflows", status_code=201, tags=["configuration"])
    def publish(workflow: Workflow):
        created = engine.store.publish(workflow, engine.clock())
        return {
            "workflow_id": workflow.workflow_id,
            "version": workflow.version,
            "created": created,
        }

    @app.get("/workflows", tags=["configuration"])
    def workflows():
        with engine.store.transaction(write=False) as connection:
            rows = connection.execute(
                "SELECT w.workflow_id,w.version,w.config_hash,w.published_at,"
                "w.version=a.version AS active FROM workflows w JOIN active_workflows a "
                "ON w.workflow_id=a.workflow_id ORDER BY w.workflow_id,w.version",
            ).fetchall()
            return [dict(row) for row in rows]

    @app.get("/workflows/{workflow_id}", tags=["configuration"])
    def workflow(workflow_id: str, version: Annotated[int | None, Query(ge=1)] = None):
        with engine.store.transaction(write=False) as connection:
            return engine.store.workflow(connection, workflow_id, version).model_dump()

    @app.post("/requests", status_code=201, response_model=RequestView, tags=["decisions"])
    def intake(
        body: Intake,
        idempotency_key: Annotated[
            str,
            Header(
                min_length=1,
                max_length=128,
                pattern=r"^[A-Za-z0-9._:-]+$",
            ),
        ],
    ):
        request, replay = engine.submit(body.workflow_id, body.payload, idempotency_key)
        result = public(engine.run(request["id"]))
        return JSONResponse(
            status_code=200 if replay else 201,
            content=result,
            headers={
                "Location": f"/requests/{result['id']}",
                "Idempotency-Replayed": str(replay).lower(),
            },
        )

    @app.get("/requests", response_model=list[RequestView], tags=["decisions"])
    def requests(
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        with engine.store.transaction(write=False) as connection:
            rows = connection.execute(
                "SELECT id FROM requests ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [public(engine.store.request(connection, row["id"])) for row in rows]

    @app.get("/requests/{request_id}", response_model=RequestView, tags=["decisions"])
    def get_request(request_id: str):
        return public(engine.get(request_id))

    @app.get("/requests/{request_id}/audit", tags=["explanations"])
    def audit(request_id: str):
        with engine.store.transaction(write=False) as connection:
            engine.store.request(connection, request_id)
            return engine.store.events(connection, request_id)

    @app.get("/requests/{request_id}/explanation", tags=["explanations"])
    def explain(request_id: str):
        explanation = engine.explain(request_id)
        explanation["request"] = public(explanation["request"])
        return explanation

    @app.post("/requests/{request_id}/resolve", response_model=RequestView, tags=["decisions"])
    def resolve(request_id: str, body: Resolution):
        return public(engine.resolve(request_id, body.decision, body.actor, body.reason))

    return app
