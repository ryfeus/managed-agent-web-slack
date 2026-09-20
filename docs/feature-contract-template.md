# Feature Contract — <name>

## Goal

## Non-goals

## User-visible behavior

## Inputs

## Outputs / side effects

## Invariants

-

## Failure semantics

- Before an external side effect:
- After an external side effect:
- Retry behavior:

## Concurrency / idempotency semantics

- Duplicate delivery:
- Ordering:
- Concurrent execution:

## Security / authorization constraints

-

## Persistence constraints

-

## Compatibility constraints

-

## Verification mapping

| Requirement | Test / verifier |
|---|---|
| | |

## Live-contract requirements

List only behavior that cannot be proven locally.

## Known limitations

Reference stable IDs from `knowledge/known-limitations.json` where applicable.

## Completion checklist

- [ ] Each behavioral requirement maps to a test or verifier.
- [ ] Failure, duplicate, concurrency, and authorization behavior is explicit.
- [ ] `./scripts/verify --fast` passes.
- [ ] `./scripts/verify --full` passes.
- [ ] Any required live-provider verification is recorded separately.
