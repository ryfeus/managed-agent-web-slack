# Feature Contract — Durable CMA A2A controller

## Goal

Accept durable A2A tasks and execute serialized turns in one CMA session per context, independently of the current web and Slack handlers.

## Non-goals

No web or Slack migration, AG-UI bridge, generic push sink, public A2A ingress, or application thread lookup.

## User-visible behavior

Current product surfaces are unchanged. A local HTTP+JSON client can submit, inspect, stream, subscribe to, approve, and cancel controller tasks.

## Inputs

Text-only USER messages require `messageId`; ordinary turns omit `taskId`. A structured `human-input/v1` response carries the existing task and context IDs and requires A2A extension negotiation. A nonstreaming send waits for a terminal or input-required boundary by default; `returnImmediately=true` returns the current Task after durable admission.

## Outputs / side effects

Admission returns a SUBMITTED or REJECTED A2A Task and schedules a controller-only wakeup. A bounded scheduler step creates or continues a CMA session. Task reads use the pending-input object until CMA accepts the user message, then rebuild Messages from CMA events. Push notification configuration is unsupported.

## Invariants

- Application `thread_id`, A2A `contextId`, and A2A `taskId` remain distinct. The controller receives no principal or surface identity.
- CMA is the canonical transcript and execution store. DSQL holds only identity, order, lease, provider event boundaries, lifecycle, and HITL decision metadata.
- Pending input text is encrypted in private S3 until CMA accepts it; DSQL and SQS carry only its opaque object key or context identity. Rejected, canceled, and failed tasks that never reached CMA retain the referenced object so their input history remains reconstructable.
- One ordinary CMA turn is active per context. Accepted turns are dispatched in controller sequence order.

## Failure semantics

- Before a CMA side effect, a task and its pending S3 object survive restart; an enqueue failure is retried through the same `messageId`. A disconnected blocking HTTP client does not cancel the durable task.
- After an ambiguous create, send, confirmation, or interrupt, reconcile provider history before any further action. If acceptance cannot be established, hold and alert rather than repeat the side effect. Controller-only admin commands inspect holds, fail them, or retry ambiguous input delivery after external verification that CMA did not accept it.
- Provider webhooks are verified hints. Duplicates and reordered delivery cause fresh state reads and harmless wakeups.
- The daily sweep deletes only old S3 objects with no DSQL reference. It alerts on nonterminal referenced pending input older than one day.

## Concurrency / idempotency semantics

- A duplicate first `messageId` returns its existing context and task; a later duplicate `(contextId, messageId)` returns its task.
- Context sequence allocation and scheduler lease claims are atomic. Every scheduler mutation checks the current UUID claim token and unexpired lease.
- A pending approval is resolved by the first valid decision. Same-decision retries return the current task; conflicting decisions fail.
- A new ordinary turn during `INPUT_REQUIRED` is persisted as REJECTED. A queued cancellation has no CMA side effect.
- `human-input/v1` metadata is rendered only when requested through the A2A extension header. A Message URI alone cannot activate a structured continuation.

## Security / authorization constraints

The A2A app has no public production ingress in this phase. Future callers must authenticate at a private control-plane boundary. The pending-input bucket blocks public access, requires TLS, and uses SSE-S3; IAM is scoped to the scheduler's required objects and queue.

## Persistence constraints

No prompt, assistant text, tool output, reasoning, or normalized A2A Task JSON is written to DSQL. Event pagination must retain histories beyond 100 CMA events. Ordinary final answers are A2A Messages, not Artifacts.

## Compatibility constraints

The existing verified Anthropic webhook sends controller sessions to SQS and preserves the legacy EventBridge path for other sessions. Existing web and Slack handlers remain direct CMA callers. The Agent Card advertises `human-input/v1`, streaming, and HTTP+JSON; full `async-copilot-controller/v1` waits for Phase 3 push.

## Verification mapping

| Requirement | Test / verifier |
|---|---|
| SDK HTTP+JSON, Agent Card, blocking and async send, get/list/stream/subscribe | Official A2A client Postgres tests |
| Extension negotiation and malformed wire requests | Official A2A client Postgres tests |
| Pending-input history and local scheduler startup | Postgres and unit tests |
| FIFO, claim fencing, first-message race | Real Postgres concurrency tests |
| CMA response loss, HITL, cancellation, >100 events | Fake-provider Postgres tests |
| S3 encryption, metadata-only wakeup, history boundary | Unit tests |
| Webhook ownership and legacy fallback | Signed webhook unit tests |
| Schema and dependency boundaries | Architecture and persistence checker self-tests |
| Existing web and Slack behavior | `./scripts/verify --full` |

## Live-contract requirements

Real CMA event shapes, cancellation while blocked on tools, and Aurora DSQL compatibility need separate live verification before production ingress or cutover.

## Known limitations

`KL-003` records the safe but potentially indefinite hold when CMA offers no idempotency key or authoritative send outcome. Phase 3 supplies generic push and the full application profile.

The local/admin CLI can list or inspect held tasks, fail one, or retry an ambiguous input delivery only after external verification that CMA did not accept it. This manual recovery does not replace provider-side idempotency.

`KL-004` records that the pinned official A2A client stops its iterator after a streamed Message, even though the HTTP+JSON server emits a later terminal status update. Clients can read final state with GetTask; the raw wire test proves the terminal event is present.

CMA budget exhaustion ends the task as FAILED with a stable status message. The controller cannot raise a CMA session budget through A2A in this phase.

## Completion checklist

- [x] `./scripts/verify --fast` passes.
- [x] `./scripts/verify --full` passes.
