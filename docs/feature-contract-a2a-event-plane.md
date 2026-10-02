# Feature Contract — A2A push event plane

## Goal

Let application threads submit controller-owned tasks and receive generic, current-state A2A change events through one private callback.

## Non-goals

No Slack or Web migration, public A2A ingress, production callback route, or transcript projection.

## User-visible behavior

Existing surfaces are unchanged. A local application thread can send a turn, inspect or cancel its Task, and observe metadata-only completion events.

## Inputs

The application sends an A2A message with a stable client `messageId` and `returnImmediately=true`. Controllers accept only the configured exact callback URL. The sink accepts standard `StreamResponse` JSON with an application-profile `X-A2A-Delivery-Id` header.

## Outputs / side effects

The controller stores a per-task push config, POSTs a bounded current Task snapshot, and retries transient failures. The sink resolves the Task binding, fetches current Task state, updates a lightweight observation, and publishes `A2ATaskUpdated` from `app.a2a`.

## Invariants

- Controller tables and push messages contain no thread, principal, or surface identities.
- Application code uses only A2A identities and types; it does not read CMA sessions or provider events.
- DSQL holds routing, claims, fingerprints, receipts, and state names, never Task JSON or conversational content.
- A push is a wakeup. `GetTask` supplies current truth, and intermediate states may coalesce.
- An Agent Card advertises push only when the callback policy and a runnable local or SQS worker are configured.
- Local maintenance runs every 60 seconds; AWS maintenance is scheduled every minute.

## Failure semantics

- Before an external side effect: logical send, context claim, and Task binding are durable and retry safe.
- After Task admission: losing the response is recovered with the same A2A `messageId`; push setup failure leaves the Task valid and yields a retryable error.
- After callback failure: the controller reconstructs the current Task at retry time. The maintenance scan also checks previously delivered configs for state drift.
- A definitive callback rejection marks the config permanently failed immediately. Operators can inspect and reset a live, unclaimed config with `list-failed-push`, `inspect-push`, and `retry-push`.
- A definitive A2A send rejection releases a first-context claim immediately. Ambiguous transport failures retain it for same-`messageId` recovery.
- After EventBus failure: the receipt remains unpublished and the sink returns non-2xx. Publication followed by a lost receipt update may duplicate an event.

## Concurrency / idempotency semantics

- `(thread_id, client_message_id)`, controller `(contextId, messageId)`, and `(agent_id, delivery_id)` are the three retry identities.
- A first-turn context claim prevents concurrent messages from creating multiple controller contexts. A busy caller waits briefly, then receives a retryable result.
- Push delivery and sink observation use separate UUID leases. A deletion waits for a claimed callback to settle before returning.
- Consumers must deduplicate `A2ATaskUpdated` by `deliveryId` and fetch current Task state before projecting content.

## Security / authorization constraints

The controller checks one configured URL, requires HTTPS when configured in production, and rejects callback authentication fields it cannot honor. The AWS queue, worker, and schedule have no public controller or sink ingress. Future private ingress must enforce the trusted network boundary before production use.

## Persistence constraints

Migration 005 adds `cma_push_configs`, `a2a_task_event_receipts`, and per-task observation claims. Push payloads, messages, tool calls, and reasoning are never persisted.

## Compatibility constraints

The application uses pinned `a2a-sdk==1.1.4` v1 HTTP+JSON operations. Legacy Web and Slack handlers retain their direct CMA paths without dual writes.

## Verification mapping

| Requirement | Test / verifier |
|---|---|
| v1 push CRUD, inline config, Agent Card | Official-client Postgres integration tests |
| Task binding, callback routing, current GetTask, generic event | Local Postgres/ASGI tracer bullet |
| Duplicate, stale, failure, and maintenance behavior | Controller and sink fault tests; normal-composition Postgres test |
| Boundary and transcript-free schema | Architecture and persistence rails |
| Legacy behavior | `./scripts/verify --full` |

## Live-contract requirements

Real CMA event cadence, Aurora DSQL behavior, and a private production network path require separate live verification before cutover. Provisioned AWS push resources alone do not establish production callback compatibility.

## Known limitations

Existing `KL-003` and `KL-004` remain applicable; Phase 3 does not change provider-side ambiguity or the pinned streaming client's terminal iterator behavior.

## Completion checklist

- [x] Each behavioral requirement maps to a test or verifier.
- [x] Failure, duplicate, concurrency, and authorization behavior is explicit.
- [x] `./scripts/verify --fast` passes.
- [x] `./scripts/verify --full` passes.
- [x] Live-provider verification is recorded separately.
