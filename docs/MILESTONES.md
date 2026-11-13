# Delivery plan and action prompts

These prompts translate the supplied hackathon brief into reviewable engineering work.
They are project instructions, not a claim that each requirement has already passed.
Each milestone is a separate Git commit; final evidence is in `docs/RESULTS.md`.

**Status: all five milestones completed and pushed.** The results report maps each
milestone to its commit and records local verification plus a successful GitHub Actions run.

## M1 — Frame the problem and repository

**Action prompt:** Design a generic configurable decision service. Map every required
capability to a component and a verification scenario. Define lifecycle, transaction
boundaries, idempotency semantics, configuration versioning, retry policy and limits.
Create the repository and capture assumptions before implementing the engine.

**Acceptance:** checked-in plan, requirements matrix, architecture and runnable package scaffold.

## M2 — Implement configuration and explainable rules

**Action prompt:** Implement a strictly validated, versioned workflow format, a safe
rule interpreter, and two independent business workflows. Reject invalid graphs and
unsupported operators. Explain every evaluated leaf condition without executing code
from configuration. Test branching and malformed configurations.

**Acceptance:** loan approval and vendor onboarding configs; rule/config unit tests pass.

## M3 — Implement durable execution and recovery

**Action prompt:** Persist immutable configurations, pinned request versions, dependency
results, lifecycle history and append-only audit events. Make state changes and their
audit atomic. Implement bounded retries, deterministic dependency simulation, manual
review, concurrent-safe idempotency and a worker that recovers committed queued work.

**Acceptance:** integration tests cover duplicates, retry timing/exhaustion, manual
review, restart recovery, rollback on partial save failure and configuration changes.

## M4 — Expose and exercise the REST interface

**Action prompt:** Add intake, status, explanation, workflow publication, audit and
manual resolution endpoints. Expose OpenAPI documentation and a retry worker CLI.
Add optional bearer authentication, controlled error responses and payload limits.
Provide a repeatable demo that uses a running HTTP server.

**Acceptance:** REST contract tests and live HTTP demo pass; OpenAPI is accessible.

## M5 — Verify, document and publish the handoff

**Action prompt:** Run the complete tests with coverage, lint, packaging and the live
demo. Record actual outputs and decision explanations, defend design trade-offs,
document scaling and operational limitations, add CI, and push each milestone.

**Acceptance:** clean checks, reproducible README, architecture/config/API docs,
example input/trace/output, results report and GitHub link.
