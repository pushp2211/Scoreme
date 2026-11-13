# DecisionForge · Scoreme assignment

## Results

The assignment implementation is complete. Results below were verified for
implementation commit [`a3b88e1`](https://github.com/pushp2211/Scoreme/commit/a3b88e10215732224be3703fda9e575058aca524)
on **13 November 2026** using Python 3.12.14.

| Validation | Result |
| --- | --- |
| Automated tests | **89 passed** |
| Statement coverage | **99.67%** (598/600 statements; CI requires at least 95%) |
| Live HTTP acceptance demo | **15 checks passed**, using separate API and worker processes |
| Lint and formatting | Passed |
| Packaging | Wheel and source archive built; 89 tests also passed from the extracted archive |
| GitHub Actions | [Verified successful run](https://github.com/pushp2211/Scoreme/actions/runs/43417266133) |

Verified scenarios include approval, rejection, manual review, transient/permanent
dependency failures, retry exhaustion, duplicate requests, concurrent execution,
restart recovery, partial-save rollback, audit history and rule/schema changes with
version pinning. Both loan approval and vendor onboarding run through the same engine.
The external verification provider is simulated, as required by the assignment.

See the [detailed results report](docs/RESULTS.md),
[decision explanations](docs/EXAMPLES.md), and
[recorded input/trace/output evidence](docs/examples/live-demo.json).

## How to validate

### 1. Check the independent CI run

Open [GitHub Actions](https://github.com/pushp2211/Scoreme/actions), select the latest
**Verify DecisionForge** run for the branch/commit under review, and confirm it is
green. Expand the test, coverage, build and HTTP demo steps to inspect their output.
The run also provides downloadable package and live-demo artifacts.

### 2. Reproduce the results locally

Requires **Python 3.12+**. Run the following in a terminal; no database server, cloud
account, AI key or running API is required:

```bash
git clone https://github.com/pushp2211/Scoreme.git
cd Scoreme
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .

pytest --cov=decisionforge --cov-fail-under=95 --cov-report=term-missing
ruff check src tests scripts
ruff format --check src tests scripts
python -m build
python scripts/demo.py --output work/live-demo.json
```

On Windows, use `python` and `.venv\Scripts\Activate.ps1` instead of the Python/activation
commands above. The lock file includes development tools and runtime packages.

Expected: **89 passing tests**, **99.67% statement coverage**, successful lint/format
and build commands, and **15 checks passed** from the demo. Every command should exit
with code 0; a failed assertion or coverage below 95% causes a nonzero exit status.
The demo creates a temporary database, starts a real HTTP API and separate worker,
saves complete explanations to `work/live-demo.json`, and stops its processes.

### 3. Inspect the API and business outcomes

Follow [Run locally](#run-locally) to start the API and retry worker, then open
**http://127.0.0.1:8000/docs**. Use [Submit a decision](#submit-a-decision) for the exact
request body, idempotency header, manual-resolution request and explanation endpoints.

| Scenario to validate | Expected behavior |
| --- | --- |
| Valid baseline loan | `APPROVED`, with a rule trace and provider result |
| Applicant age 17 | `REJECTED`, explained by the `underage` rule |
| Loan amount 200000 | `MANUAL_REVIEW`; reviewer decision and reason are audited |
| `simulation: {"fail_until": 1}` | `RETRY_PENDING`, then `APPROVED` after the worker retries |
| Same input and idempotency key twice | Same request ID; replay returns HTTP 200 |
| Same key with different input | HTTP 409 conflict |
| Publish changed rules as a new version | New requests use new rules; earlier requests retain their version |

Use a fresh idempotency key for each independent scenario. The automated suite also
checks retry exhaustion, concurrent duplicates, restart recovery and rollback after
an injected partial-save failure. Follow the [configuration change recipe](docs/CONFIGURATION.md#a-policy-change-end-to-end)
to verify a policy update without editing engine code.

## About the project

A configurable workflow decision platform built for the **Resilient Decision System**
hackathon assignment. It accepts structured requests, applies explainable rules,
executes stages, persists state, and recovers from failures with bounded retries.

**Python · FastAPI · SQLite · JSON Schema · YAML workflows**

- Two configurable use cases: loan approval and vendor onboarding.
- Immutable workflow versions; in-flight requests keep their original rules.
- Concurrent-safe idempotency, persisted retries, manual review and restart recovery.
- Atomic state/audit writes, sequenced append-only audit events and decision explanations.
- A safe rule interpreter: no executable expressions or code in configuration.

## Run locally

Complete the installation in [How to validate](#how-to-validate) first. From the
repository root with the virtual environment activated, start the API:

```bash
decisionforge serve
```

Open **http://127.0.0.1:8000/docs** for the interactive REST interface. The server seeds
both workflow configurations, and stores decisions in `data/decisionforge.db`.
In a second activated terminal, start the worker so retries and queued work progress:

```bash
decisionforge worker
```

The API performs the initial execution immediately. A worker is required for automatic
retry recovery; a replay of the same intake request can also process a due retry.
Stop either process with Ctrl+C. Restarting preserves requests, retries and audit logs.

## Submit a decision

```bash
curl -i http://127.0.0.1:8000/requests \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: company-demo-001' \
  -d '{"workflow_id":"loan_approval","payload":{"applicant_id":"A-001","age":30,"income":60000,"amount":25000,"consent":true}}'
```

Expected: **201**, `state: APPROVED`, a request `id`, and a `Location` header.
Repeat the exact request/key: **200**, same ID, `Idempotency-Replayed: true`.
Reuse that key with different input: **409**. The key is global across workflows.
JSON object key order is ignored; numeric representations are not coerced (`1` and
`1.0` can produce different hashes). Keys persist for the life of the database.

Use the returned ID:

```bash
curl http://127.0.0.1:8000/requests/REQUEST_ID
curl http://127.0.0.1:8000/requests/REQUEST_ID/explanation
curl http://127.0.0.1:8000/requests/REQUEST_ID/audit
```

Try these payload changes with a **new key each time**:

| Change to the example | Outcome |
| --- | --- |
| `age: 17` | `REJECTED`, explained by `underage` rule |
| `amount: 200000` | `MANUAL_REVIEW` |
| `simulation: {"fail_until": 1}` | `RETRY_PENDING`, then `APPROVED` after worker retry |
| `simulation: {"fail_until": 10}` | Three total attempts, then `MANUAL_REVIEW` |
| `simulation: {"permanent_failure": true}` | Immediate `MANUAL_REVIEW` |
| `simulation: {"verified": false}` | `REJECTED` |

The verification provider is explicitly a **simulation**. Its default response is
verified identity with risk score 10. Production identity verification is outside
this submission; see the adapter and transaction trade-offs in the architecture.

Resolve a manual review:

```bash
curl http://127.0.0.1:8000/requests/REQUEST_ID/resolve \
  -H 'Content-Type: application/json' \
  -d '{"decision":"approve","actor":"reviewer-001","reason":"Supporting documents checked"}'
```

## Change a workflow without changing engine code

Copy a YAML file in `configs/`, increase its `version`, edit rules/stages/schema,
and publish it:

```bash
decisionforge publish configs/loan_approval.v2.yaml
```

The path above is an example for a version you create; the repository ships v1.
Alternatively send the equivalent JSON to `POST /workflows`. Existing versions cannot
be mutated; new requests use the latest published version. Re-publishing an identical
version is safe and does not reactivate an older version. Merely editing a file does
not change the running engine; publish it explicitly or restart with the new config.

See [configuration reference](docs/CONFIGURATION.md) for the full format, operators,
version-change recipe, graph restrictions and retry policy.

## API and operations

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/health` | Database readiness check |
| POST / GET | `/workflows` | Publish / list immutable workflow versions |
| GET | `/workflows/{id}?version=1` | Read active or historical configuration |
| POST / GET | `/requests` | Idempotent intake / paginated list |
| GET | `/requests/{id}` | Current persisted state |
| GET | `/requests/{id}/explanation` | Input, config, rule trace and complete audit |
| GET | `/requests/{id}/audit` | Sequenced audit events |
| POST | `/requests/{id}/resolve` | Resolve the current manual stage |
| GET | `/docs`, `/openapi.json` | Interactive docs / machine-readable contract |

Default binding is loopback. Set `DECISIONFORGE_API_TOKEN` before starting the server
to require `Authorization: Bearer YOUR_TOKEN` on **all** endpoints, including health
and docs. Do not commit tokens. With authentication enabled, fetch docs/OpenAPI using
a client that can attach that header. This shared token does not implement user roles;
the reviewer actor is caller-declared. Keep the default unauthenticated mode local.

| Setting / command | Default / meaning |
| --- | --- |
| `DECISIONFORGE_DB` or global `--db` | `data/decisionforge.db` |
| `DECISIONFORGE_CONFIG_DIR` / serve `--configs` | `configs` |
| serve `--host`, `--port` | `127.0.0.1`, `8000` |
| worker `--interval`, `--limit` | 1 second, 100 candidates per poll |
| `decisionforge worker --once` | One batch; prints selected candidate count |
| `decisionforge --db path/to/file.db worker` | Global options precede the command |

Keep API and worker on the **same database path**. No worker is started inside API
processes. Execution errors roll back and remain eligible for later polls; a worker
logs a failed request and continues processing the others. A persistent unexpected
error needs an operator/code fix; only simulated transient dependency errors consume
the configured retry budget. Back up the SQLite database using its backup API while
running, or stop processes before copying the database and its WAL files.

Requests are limited to 1 MiB. Validation errors return 422; key/version/state
conflicts return 409; missing resources return 404; storage errors return 503. After
503, retry intake using the **same key**: it may already have committed queued work.
No user-facing result is committed without its matching audit history.

## Design and delivery

- [Architecture, data flow, consistency, assumptions and scaling](docs/ARCHITECTURE.md)
- [Milestone action prompts and acceptance criteria](docs/MILESTONES.md)
- [Configuration model and policy changes](docs/CONFIGURATION.md)
- [Decision explanation examples](docs/EXAMPLES.md)
- [Verification report](docs/RESULTS.md)

The implementation intentionally uses single-host SQLite and an in-process provider
simulation. It does not claim production throughput, real third-party verification,
exactly-once network effects, or administrator-proof audit storage. The architecture
explains the PostgreSQL/queue/outbox path for those requirements.
