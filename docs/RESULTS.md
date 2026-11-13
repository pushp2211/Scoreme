# Verification and handoff report

Verified on **13 November 2026**, using Linux and **Python 3.12.14**.
Repository: **https://github.com/pushp2211/Scoreme**. Project name: **DecisionForge**.

## Local verification

| Check | Actual result |
| --- | --- |
| Automated tests | **89 passed** |
| Statement coverage | **99.67%** — 598/600 statements, with a 95% CI gate |
| Ruff lint | Passed |
| Ruff formatting | All 17 Python files formatted |
| `git diff --check` | Passed |
| `pip check` | No broken requirements |
| Clean virtual environment | Install from project dependency declarations and full test suite passed |
| Packaging | Built wheel and source distribution successfully |
| Separate-process live HTTP demo | **15 checks passed** |

The exact dependency versions are in `requirements.lock`. A fresh virtual environment
was used to resolve this lock and run the full tests. `.github/workflows/ci.yml`
repeats lint, format, coverage, package build and live HTTP acceptance checks on GitHub.
The latest run status is visible in the repository's **Actions** tab.

## Verified behavior

- Approval and rejection with actual/expected rule values and input/config hashes.
- Mandatory input/schema checks, invalid JSON, non-finite numbers, body limits,
  malformed configurations, unsupported references/operators and graph cycles.
- Exact duplicate replay, payload conflict and 16 concurrent submissions/executions
  yielding one request and one provider success.
- Persisted retry due times, no early retry, capped exponential backoff, success
  after a transient outage, bounded exhaustion and immediate permanent-error fallback.
- Independent attempt budgets across multiple dependency stages.
- Human review and decisions with actor/reason audit; concurrent resolutions permit
  only one winner.
- Recovery after process restart and after intake committed before execution.
- Injected partial-save failure after state write but before audit completion: the
  entire execution rolls back to its prior durable state and recovers on retry.
- Manual resolution rollback, and worker isolation of a failed request so other
  queued requests still progress.
- Changed rules/schema apply to new requests while in-flight requests and original
  idempotency replays preserve their pinned version.
- Database-level prevention of audit and configuration update/delete.
- REST authentication, resource/status endpoints, publication, manual review,
  audit/explanation endpoints and OpenAPI contract.
- CLI publication, one-shot and continuous worker operation, graceful interrupt,
  invalid options, configuration reseeding and module entry point.

The two uncovered statements are the defensive engine execution-bound guard (validated
acyclic graphs cannot reach it) and a redundant direct Pydantic-validation error
handler. This is statement coverage, not a claim of complete state-space or load testing.

## Real HTTP evidence

The checked-in [`examples/live-demo.json`](examples/live-demo.json) was produced by
`python scripts/demo.py --output docs/examples/live-demo.json`. A fresh second run
also passed all 15 checks. Each run creates a temporary database and starts a real
Uvicorn process plus a separate durable worker. The results include ten full snapshots:

| Snapshot | Result |
| --- | --- |
| approval | APPROVED |
| rejection | REJECTED |
| manual_pending | MANUAL_REVIEW |
| manual_approved | APPROVED, with reviewer reason |
| retry_pending | RETRY_PENDING, persisted due time |
| retry_recovered | APPROVED, second dependency attempt |
| permanent_failure | MANUAL_REVIEW, one failed attempt |
| vendor_approval | APPROVED, second business configuration |
| policy_v1 | APPROVED |
| policy_v2 | REJECTED after a configured threshold change |

The other five checks cover replay, key conflict, schema rejection, version pinning
and OpenAPI. [Explanation examples](EXAMPLES.md) explain the raw traces in business terms.

## Milestone delivery

| Milestone | Commit / evidence |
| --- | --- |
| M1: problem framing and architecture | `21dd74a` |
| M2: configuration and rules | `8de4f39` |
| Repository integration, preserving original contents/history | `1fb9f44` |
| M3: durable engine and recovery | `cd8b849` |
| M4: REST interface and live acceptance demo | `e365dc2` |
| M5: expanded verification and company handoff | Commit containing this report; see Git history |

All milestone action prompts and acceptance criteria are in [MILESTONES.md](MILESTONES.md).
The existing Scoreme repository was used because the connected GitHub integration
could push to it but could not create a new repository. Original history and ignore
patterns were preserved; the initial placeholder README was expanded into the guide.

## Practical limits

This is a runnable, verified hackathon submission. Provider calls are simulated and
client simulation controls are demo-only. SQLite supports one host and serializes
writers; there is no measured production capacity. Shared bearer authentication is
optional and does not provide RBAC. Caller-declared reviewer names are not identity
verification. Audit events are append-only to the application, not tamper-proof against
administrators. Real remote side effects require an outbox, leases and downstream
idempotency rather than a claim of exactly-once execution. Schemas are self-contained
and workflow graphs are acyclic. See [ARCHITECTURE.md](ARCHITECTURE.md) for the reasoning
and scaling plan.

The wheel contains the engine/API/CLI; sample YAML configurations and the demonstration
scripts are repository assets. Run from the checkout, or provide the configuration
folder explicitly with `decisionforge serve --configs /path/to/configs`.
