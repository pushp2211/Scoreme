"""SQLite unit-of-work and append-only persistence primitives."""

import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from decisionforge.config import Workflow, canonical, digest
from decisionforge.errors import DomainError

SCHEMA = """
CREATE TABLE IF NOT EXISTS workflows (
    workflow_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    config TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    published_at REAL NOT NULL,
    PRIMARY KEY (workflow_id, version)
);
CREATE TABLE IF NOT EXISTS active_workflows (
    workflow_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    FOREIGN KEY (workflow_id, version) REFERENCES workflows(workflow_id, version)
);
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    input_hash TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    workflow_version INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN
      ('QUEUED','RUNNING','RETRY_PENDING','MANUAL_REVIEW','APPROVED','REJECTED')),
    stage TEXT NOT NULL,
    payload TEXT NOT NULL,
    dependencies TEXT NOT NULL DEFAULT '{}',
    attempts TEXT NOT NULL DEFAULT '{}',
    next_attempt_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (workflow_id, workflow_version) REFERENCES workflows(workflow_id, version)
);
CREATE INDEX IF NOT EXISTS requests_due ON requests(state, next_attempt_at, created_at);
CREATE TABLE IF NOT EXISTS events (
    request_id TEXT NOT NULL REFERENCES requests(id),
    sequence INTEGER NOT NULL,
    timestamp REAL NOT NULL,
    kind TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (request_id, sequence)
);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS workflows_no_update BEFORE UPDATE ON workflows
BEGIN SELECT RAISE(ABORT, 'workflow versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS workflows_no_delete BEFORE DELETE ON workflows
BEGIN SELECT RAISE(ABORT, 'workflow versions are immutable'); END;
"""


class Store:
    def __init__(self, path: str):
        if path == ":memory:":
            raise ValueError("use a filesystem database: each transaction has its own connection")
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def transaction(self, *, write=True):
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def publish(self, workflow: Workflow, now: float):
        config = workflow.model_dump()
        config_hash = digest(config)
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT config_hash FROM workflows WHERE workflow_id=? AND version=?",
                (workflow.workflow_id, workflow.version),
            ).fetchone()
            if existing:
                if existing["config_hash"] != config_hash:
                    raise DomainError(
                        "version_conflict", "Published versions cannot be modified", 409
                    )
                return False
            active = connection.execute(
                "SELECT version FROM active_workflows WHERE workflow_id=?",
                (workflow.workflow_id,),
            ).fetchone()
            if active and workflow.version <= active["version"]:
                raise DomainError(
                    "version_conflict", "New versions must increase monotonically", 409
                )
            connection.execute(
                "INSERT INTO workflows VALUES (?,?,?,?,?)",
                (
                    workflow.workflow_id,
                    workflow.version,
                    canonical(config),
                    config_hash,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO active_workflows VALUES (?,?) ON CONFLICT(workflow_id) "
                "DO UPDATE SET version=excluded.version",
                (workflow.workflow_id, workflow.version),
            )
        return True

    @staticmethod
    def workflow(connection, workflow_id: str, version: int | None = None) -> Workflow:
        if version is None:
            row = connection.execute(
                "SELECT w.config FROM workflows w JOIN active_workflows a "
                "ON w.workflow_id=a.workflow_id AND w.version=a.version WHERE w.workflow_id=?",
                (workflow_id,),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT config FROM workflows WHERE workflow_id=? AND version=?",
                (workflow_id, version),
            ).fetchone()
        if not row:
            raise DomainError("workflow_not_found", "Workflow version does not exist", 404)
        return Workflow.model_validate(json.loads(row["config"]))

    @staticmethod
    def request(connection, request_id: str) -> dict:
        row = connection.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            raise DomainError("request_not_found", "Request does not exist", 404)
        result = dict(row)
        for key in ("payload", "dependencies", "attempts"):
            result[key] = json.loads(result[key])
        return result

    @staticmethod
    def save(connection, request: dict):
        connection.execute(
            "UPDATE requests SET state=?,stage=?,dependencies=?,attempts=?,next_attempt_at=?,"
            "updated_at=? WHERE id=?",
            (
                request["state"],
                request["stage"],
                canonical(request["dependencies"]),
                canonical(request["attempts"]),
                request["next_attempt_at"],
                request["updated_at"],
                request["id"],
            ),
        )

    @staticmethod
    def append_event(connection, request_id: str, now: float, kind: str, data: dict):
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM events WHERE request_id=?",
            (request_id,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO events VALUES (?,?,?,?,?)",
            (request_id, sequence, now, kind, canonical(data)),
        )

    @staticmethod
    def events(connection, request_id: str) -> list[dict]:
        rows = connection.execute(
            "SELECT * FROM events WHERE request_id=? ORDER BY sequence", (request_id,)
        ).fetchall()
        return [{**dict(row), "data": json.loads(row["data"])} for row in rows]
