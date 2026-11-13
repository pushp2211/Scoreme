"""Server, worker and configuration commands."""

import argparse
import json
import logging
import os
import time
from pathlib import Path

import uvicorn

from decisionforge.config import load_workflow
from decisionforge.engine import Engine
from decisionforge.storage import Store

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="DecisionForge workflow platform")
    parser.add_argument("--db", default=os.getenv("DECISIONFORGE_DB", "data/decisionforge.db"))
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Run the REST API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--configs", default=os.getenv("DECISIONFORGE_CONFIG_DIR", "configs"))
    worker = commands.add_parser("worker", help="Process queued requests and due retries")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--interval", type=float, default=1.0)
    worker.add_argument("--limit", type=int, default=100)
    publish = commands.add_parser("publish", help="Publish an immutable YAML workflow version")
    publish.add_argument("path", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.command == "serve":
        from decisionforge.api import create_app

        uvicorn.run(
            create_app(db_path=args.db, config_dir=args.configs), host=args.host, port=args.port
        )
        return
    store = Store(args.db)
    if args.command == "publish":
        workflow = load_workflow(args.path)
        created = store.publish(workflow, time.time())
        print(
            json.dumps(
                {
                    "workflow_id": workflow.workflow_id,
                    "version": workflow.version,
                    "created": created,
                }
            )
        )
        return
    if args.interval <= 0 or not 1 <= args.limit <= 1000:
        parser.error("worker interval must be positive and limit must be 1..1000")
    engine = Engine(store)
    try:
        while True:
            processed = engine.work_once(args.limit)
            if args.once:
                print(json.dumps({"selected": processed}))
                return
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.info("Worker stopped")
