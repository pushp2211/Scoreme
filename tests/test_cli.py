import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from decisionforge.api import create_app
from decisionforge.cli import main

ROOT = Path(__file__).resolve().parents[1]


def test_cli_publish_and_worker_once(tmp_path, monkeypatch, capsys):
    database = str(tmp_path / "cli.db")
    monkeypatch.setattr(
        sys,
        "argv",
        ["decisionforge", "--db", database, "publish", str(ROOT / "configs/loan_approval.v1.yaml")],
    )
    main()
    assert json.loads(capsys.readouterr().out)["created"] is True
    main()
    assert json.loads(capsys.readouterr().out)["created"] is False
    monkeypatch.setattr(sys, "argv", ["decisionforge", "--db", database, "worker", "--once"])
    main()
    assert json.loads(capsys.readouterr().out)["selected"] == 0


def test_cli_serve_factory_and_config_reload(tmp_path, monkeypatch):
    database = str(tmp_path / "api.db")
    called = []

    def fake_run(app, **kwargs):
        with TestClient(app) as client:
            assert len(client.get("/workflows").json()) == 2
            assert client.get("/health").status_code == 200
        called.append(kwargs)

    monkeypatch.setattr("decisionforge.cli.uvicorn.run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "decisionforge",
            "--db",
            database,
            "serve",
            "--port",
            "8888",
            "--configs",
            str(ROOT / "configs"),
        ],
    )
    main()
    main()  # Re-seeding immutable versions is safe on restart.
    assert called == [{"host": "127.0.0.1", "port": 8888}] * 2


@pytest.mark.parametrize("arguments", [["--interval", "0"], ["--limit", "0"]])
def test_cli_invalid_worker_arguments(tmp_path, monkeypatch, arguments):
    monkeypatch.setattr(
        sys, "argv", ["decisionforge", "--db", str(tmp_path / "cli.db"), "worker", *arguments]
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


def test_worker_loop_graceful_interrupt(tmp_path, monkeypatch):
    def interrupt(_):
        raise KeyboardInterrupt

    monkeypatch.setattr("decisionforge.cli.time.sleep", interrupt)
    monkeypatch.setattr(sys, "argv", ["decisionforge", "--db", str(tmp_path / "cli.db"), "worker"])
    main()


def test_missing_configuration_directory(tmp_path):
    with pytest.raises(ValueError, match="Configuration directory"):
        create_app(db_path=str(tmp_path / "empty.db"), config_dir=str(tmp_path / "missing"))


def test_module_entrypoint(tmp_path, monkeypatch, capsys):
    import runpy

    monkeypatch.setattr(
        sys, "argv", ["decisionforge", "--db", str(tmp_path / "module.db"), "worker", "--once"]
    )
    runpy.run_module("decisionforge", run_name="__main__")
    assert json.loads(capsys.readouterr().out)["selected"] == 0
