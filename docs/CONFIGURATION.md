# Configuration model

Workflows are strict YAML/JSON documents validated before publication. Unknown keys,
unknown adapters, invalid rule operators, unreachable stages, cycles and nonexistent
transition targets are rejected. IDs match `[a-z][a-z0-9_]{0,63}`. There are at most
100 stages and 100 rules per rules stage.

## Top-level contract

| Field | Meaning |
| --- | --- |
| `workflow_id` | Stable business workflow identifier |
| `version` | Positive integer; new publications must increase |
| `description` | Human-readable purpose |
| `input_schema` | Inline JSON Schema Draft 2020-12 object; references are unsupported |
| `start` | Initial stage ID |
| `retry` | `max_attempts`, `base_delay_seconds`, `max_delay_seconds` |
| `stages` | Map of stage IDs to the tagged stage definitions below |

The platform stores normalized JSON and its SHA-256 hash. The hash and version are
attached to each submitted request. Historical configuration is available through
`GET /workflows/{id}?version=N`. Published rows are database-protected against mutation.
Publication is atomic: the configuration insert and active-version pointer move together.
The `format` JSON Schema keyword is annotation-only; type, required, bounds, enum,
additionalProperties and other validation keywords are enforced.

## Stage types

```yaml
# Ordered, first-match branching. Unmatched rules continue; default handles no matches.
eligibility:
  type: rules
  rules:
    - id: underage
      description: Applicant must be at least 18
      condition: {field: input.age, op: lt, value: 18}
      on_match: rejected
  default: identity

# Adapter name is allowlisted. A permanent failure or exhausted retries uses on_failure.
identity:
  type: dependency
  adapter: simulated_verification
  next: risk
  on_failure: review

# Pause for a caller-declared reviewer, then branch using one of these decision names.
review:
  type: manual
  reason: Human review required
  decisions: {approve: approved, reject: rejected}

approved:
  type: terminal
  outcome: APPROVED
  reason: Requirements satisfied

rejected:
  type: terminal
  outcome: REJECTED
  reason: Blocking rule or reviewer decision; consult audit trace
```

This is a format illustration, not a complete graph (`risk` must be defined).
Complete runnable graphs are in `configs/`. Terminal outcomes are `APPROVED` and
`REJECTED`; retry/manual states are produced by their respective stages. Multi-stage
manual decisions and multiple verification stages use the same engine.

## Rule conditions

Leaf predicates reference dot-separated object keys under `input.*` or
`dependencies.STAGE_ID.*`. Array indexing, arithmetic, custom functions and code
execution are unsupported. Intermediate missing/non-object paths count as missing.

| Operator | Meaning |
| --- | --- |
| `exists`, `missing` | Presence of key; use no `value`. Present null still exists. |
| `eq`, `ne` | Typed JSON equality/inequality; numbers compare numerically |
| `gt`, `gte`, `lt`, `lte` | Numeric comparison; both operands must be numbers |
| `in`, `not_in` | Typed equality against elements in a configured list |

Missing data never satisfies a value comparison, including `ne` and `not_in`.
Booleans do not compare equal to numbers, including inside arrays/objects.
Groups are `all`, `any` and `not`:

```yaml
condition:
  all:
    - {field: input.consent, op: eq, value: true}
    - any:
        - {field: input.amount, op: gt, value: 100000}
        - {field: dependencies.identity.risk_score, op: gte, value: 50}
```

All children of a group are evaluated for a complete trace. A rules stage stops
at its first matching rule; later rules in that stage are intentionally not evaluated
and are not represented as false. The stored immutable workflow shows their order.
Maximum group depth is 16; `all`/`any` require 1–50 children.

## Retry and simulation contract

```yaml
retry:
  max_attempts: 3          # total attempts PER DEPENDENCY STAGE, including first call
  base_delay_seconds: 2
  max_delay_seconds: 30
```

After failed attempt `n`, delay is `min(base_delay_seconds * 2^(n-1), max_delay_seconds)`.
The due timestamp is persisted; the worker never runs a retry early. The first failure
above schedules +2 seconds, the second +4 seconds, and the third takes `on_failure`.
Attempts for later dependency stages start their own budget. Completed rules/stages
are not repeated when a dependency is retried.

`simulation` is a reserved input object. Include its schema when allowing it in a
business payload; both sample configs do so. Defaults and allowed values:

| Key | Default | Meaning |
| --- | --- | --- |
| `fail_until` | 0 | Attempts 1..N fail transiently; integer 0..10 |
| `permanent_failure` | false | Fail permanently on the first call |
| `verified` | true | Successful result's verification flag |
| `risk_score` | 10 | Successful result's risk score, integer 0..100 |

Permanent failures take precedence over `fail_until`. Results are saved under the
current dependency stage ID. The operation key is `request_id:stage_id`, stable across
attempts. The simulator does not contact an actual external provider and does not
pretend to verify real people or vendors. In production, remove client-controlled
simulation input and add an authenticated, timeout-bounded provider adapter with an
outbox and downstream idempotency.

## A policy change, end to end

1. Copy `configs/loan_approval.v1.yaml` to `configs/loan_approval.v2.yaml`.
2. Keep `workflow_id: loan_approval`; set `version: 2`.
3. Change `income_below_minimum.condition.value` from `30000` to `50000`.
4. Publish: `decisionforge publish configs/loan_approval.v2.yaml`.
5. Submit age 30, income 40000, amount 25000, consent true with a fresh key.
   It is rejected by v2; the same input was approved by v1.
6. Read an earlier request: its version and trace remain v1. Replaying its original
   idempotency key returns it, even if v2 has introduced new required fields.

No engine source changes are needed. New adapter types and new DSL operators are
intentional code extensions; business thresholds and stage routing are configuration.
