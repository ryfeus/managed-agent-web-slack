# Feature Contract — A2A foundation

## Goal

Add inert A2A/AG-UI contracts and persistence for later controller and surface migrations.

## Non-goals

No A2A server, AG-UI bridge, handler migration, controller scheduler, deployment, or dual writes.

## User-visible behavior

Existing web and Slack behavior is unchanged.

## Inputs

The optional `CMA_A2A_ENDPOINT` enters through `AppConfig` and configures the static `cma` registration. New repository methods accept application thread, surface, logical-send, and controller-private identities according to their owning boundary.

## Outputs / side effects

New modules expose protocol contracts and repository operations. Migration 003 creates five additive tables.

## Invariants

- Application `thread_id`, A2A `contextId`, and A2A `taskId` are distinct.
- Application tables contain no transcript, messages, artifacts, tool output, or reasoning.
- CMA-private repository methods accept no principal, thread, or surface identities.
- A ready context is immutable through ordinary repository operations.

## Failure semantics

- Before an external side effect: all Phase 1 operations are database-local; unsuccessful transactions leave no partial logical send or binding.
- After an external side effect: none exists in Phase 1.
- Retry behavior: identity-binding operations are retry-safe. If a commit succeeds but its response is lost, retrying with the same stable identity returns the existing row. A conflicting identity raises an error without changing that row.

## Concurrency / idempotency semantics

- Duplicate delivery: logical sends deduplicate on `(thread_id, client_message_id)` and CMA context creation on `creation_message_id`.
- Ordering: CMA task sequence is unique within its context; no scheduler is implemented.
- Concurrent execution: one context claim token wins at a time. An expired claim can be replaced, and a stale token cannot bind or release a context.
- `bind_context(T1, claim1, C1)` retried after commit returns `C1`; `bind_task_id(T1, M1, task1)` returns `task1`; and `bind_cma_session(C1, session1)` returns `session1`.
- CMA context creation deduplicates by creation message, and CMA task creation deduplicates by `(context_id, message_id)`. Reusing a context or task ID for another message fails.

## Security / authorization constraints

- Caller-facing authorization remains in the current handlers. Future callers must validate thread ownership before invoking generic repository methods.
- A surface binding key cannot be reassigned to another thread.

## Persistence constraints

- Legacy schema remains untouched and is not dual-written.
- New application tables hold identities, routing, idempotency, and disposable projections only.
- SQL CHECK constraints require consistent thread context state, CMA context lifecycle/session state, positive CMA sequence values, and a matching logical-send status/task ID.

## Compatibility constraints

Current direct-CMA paths remain active. The pinned packages are `a2a-sdk==1.1.4` and `ag-ui-protocol==1.0.0`.

## Verification mapping

| Requirement | Test / verifier |
|---|---|
| Protocol payloads and registry | Protocol and registry unit tests |
| Schema inventory and no transcript fields | Persistence and architecture checkers |
| Retry, conflict, malformed state, and race semantics | Real Postgres integration tests |
| Controller import boundary | Architecture checker self-tests |
| Existing web and Slack behavior | `./scripts/verify --full` |

## Live-contract requirements

DSQL and real A2A controller compatibility remain for later phases; Phase 1 makes no live-provider claim.

## Known limitations

None introduced by this inert foundation.

## Completion checklist

- [x] `./scripts/verify --fast` passes.
- [x] `./scripts/verify --full` passes.
- [x] Migration repeatability and concurrency tests pass.
