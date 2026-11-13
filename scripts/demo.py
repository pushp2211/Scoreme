#!/usr/bin/env python3
"""Real HTTP acceptance demo: fresh database, API process and durable retry worker."""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_demo(base):
    token = os.getenv("DECISIONFORGE_API_TOKEN")

    def call(method, path, body=None, key=None, expected=200):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if key:
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(
            base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status, content = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, content = error.code, error.read()
        assert status == expected, (path, status, content.decode())
        return json.loads(content)

    deadline = time.monotonic() + 20
    while True:
        try:
            call("GET", "/health")
            break
        except (urllib.error.URLError, ConnectionError):
            if time.monotonic() >= deadline:
                raise TimeoutError("API did not become ready") from None
            time.sleep(0.1)

    loan = {
        "applicant_id": "demo-001",
        "age": 30,
        "income": 60000,
        "amount": 25000,
        "consent": True,
    }
    examples, checks = {}, []

    def intake(payload, key, workflow="loan_approval", expected=201):
        return call(
            "POST", "/requests", {"workflow_id": workflow, "payload": payload}, key, expected
        )

    def record(name, result):
        explanation = call("GET", f"/requests/{result['id']}/explanation")
        assert explanation["events"][0]["data"]["input"] == result["payload"]
        transitions = [e for e in explanation["events"] if e["kind"] == "transition"]
        assert transitions[-1]["data"]["to"]["state"] == result["state"]
        examples[name] = explanation
        print(f"PASS {name}: {result['state']}")
        checks.append(name)

    approved = intake(loan, "demo-approved")
    assert approved["state"] == "APPROVED"
    record("approval", approved)
    replay = intake(loan, "demo-approved", expected=200)
    assert replay["id"] == approved["id"]
    intake(loan | {"age": 40}, "demo-approved", expected=409)
    intake(loan | {"age": "30"}, "demo-invalid", expected=422)
    checks.extend(["idempotent_replay", "idempotency_conflict", "schema_rejection"])

    rejected = intake(loan | {"age": 17}, "demo-rejected")
    assert rejected["state"] == "REJECTED"
    record("rejection", rejected)

    manual = intake(loan | {"amount": 200000}, "demo-manual")
    assert manual["state"] == "MANUAL_REVIEW"
    record("manual_pending", manual)
    resolved = call(
        "POST",
        f"/requests/{manual['id']}/resolve",
        {
            "decision": "approve",
            "actor": "demo-reviewer",
            "reason": "Verified supporting documents",
        },
    )
    assert resolved["state"] == "APPROVED"
    record("manual_approved", resolved)

    retry = intake(loan | {"simulation": {"fail_until": 1}}, "demo-retry")
    assert retry["state"] == "RETRY_PENDING"
    record("retry_pending", retry)
    deadline = time.monotonic() + 15
    while retry["state"] == "RETRY_PENDING":
        assert time.monotonic() < deadline, "Retry worker did not finish"
        time.sleep(0.2)
        retry = call("GET", f"/requests/{retry['id']}")
    assert retry["state"] == "APPROVED" and retry["attempts"]["identity"] == 2
    record("retry_recovered", retry)

    permanent = intake(loan | {"simulation": {"permanent_failure": True}}, "demo-permanent")
    assert permanent["state"] == "MANUAL_REVIEW"
    record("permanent_failure", permanent)

    vendor = intake(
        {"vendor_id": "demo-vendor", "country": "IN", "documents": True, "annual_spend": 20000},
        "demo-vendor",
        "vendor_onboarding",
    )
    assert vendor["state"] == "APPROVED"
    record("vendor_approval", vendor)

    config = call("GET", "/workflows/loan_approval")
    config["workflow_id"] = "demo_policy_change"
    call("POST", "/workflows", config, expected=201)
    old = intake(loan, "demo-policy-v1", "demo_policy_change")
    assert old["state"] == "APPROVED"
    config["version"] = 2
    config["stages"]["risk"]["rules"][1]["condition"]["value"] = 5
    call("POST", "/workflows", config, expected=201)
    new = intake(loan, "demo-policy-v2", "demo_policy_change")
    assert new["state"] == "REJECTED"
    record("policy_v1", old)
    record("policy_v2", new)
    assert call("GET", f"/requests/{old['id']}")["workflow_version"] == 1
    checks.append("version_pinning")
    assert call("GET", "/openapi.json")["info"]["title"] == "DecisionForge"
    checks.append("openapi")
    return {
        "transport": "real HTTP / separate API and worker processes",
        "checks_passed": checks,
        "examples": examples,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("work/live-demo.json"))
    args = parser.parse_args()
    processes = []
    with tempfile.TemporaryDirectory(prefix="decisionforge-demo-") as temporary:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        database = str(Path(temporary) / "demo.db")
        prefix = [sys.executable, "-m", "decisionforge", "--db", database]
        log_path = Path(temporary) / "services.log"
        with log_path.open("w+") as logs:
            try:
                processes.append(
                    subprocess.Popen(
                        [*prefix, "serve", "--port", str(port), "--configs", str(ROOT / "configs")],
                        cwd=ROOT,
                        stdout=logs,
                        stderr=logs,
                    )
                )
                # Start worker after API schema bootstrap to avoid concurrent WAL initialization.
                deadline = time.monotonic() + 20
                while not Path(database).exists() or Path(database).stat().st_size == 0:
                    if time.monotonic() >= deadline or processes[0].poll() is not None:
                        raise RuntimeError("API failed to initialize the database")
                    time.sleep(0.1)
                processes.append(
                    subprocess.Popen(
                        [*prefix, "worker", "--interval", "0.2"],
                        cwd=ROOT,
                        stdout=logs,
                        stderr=logs,
                    )
                )
                result = run_demo(f"http://127.0.0.1:{port}")
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(result, indent=2) + "\n")
                print(f"{len(result['checks_passed'])} checks passed; saved {args.output}")
            except BaseException:
                logs.flush()
                print(log_path.read_text(), file=sys.stderr)
                raise
            finally:
                for process in reversed(processes):
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


if __name__ == "__main__":
    main()
