# Architecture

Web and Slack operate on application threads and A2A Tasks. Only the private controller calls CMA session/event APIs. Anthropic owns canonical transcripts and execution. DSQL stores identity, authorization, routing, idempotency, leases, projection state and opaque temporary-object keys; never message bodies, tool arguments/output or reasoning.

## Boundaries

- `apps/web` uses assistant-ui and AG-UI. Cookie-authenticated `agui_bridge` authorizes every thread operation and translates A2A history/status into AG-UI.
- `agent_control_plane` owns application threads, principal ownership, surface bindings and logical input IDs. Slack input and Web reach the same private A2A controller through this service.
- `cma_controller` owns private provider identity, FIFO Tasks, canonical history reconciliation, native policy and confirmation fencing. Provider IDs never appear in browser payloads.
- Signed Slack ingress hands work to EventBridge/SQS. A2A push callbacks enqueue projection notifications; the short Slack projector reads canonical `GetTask` inside a lease and deduplicates stable message IDs. It never tails CMA or subscribes to a Task.
- Production uses private A2A HTTP+JSON routes and an exact authenticated push destination. Runtime composition constructs adapters; handlers depend on ports.

## Durable execution and recovery

The earliest nonterminal Task is active. Send, history, resume, pending input and Stop select that same FIFO Task. Verified Anthropic webhooks wake the queued scheduler; a 30-second reconciliation schedule recovers missed notifications. Scheduler workers hold a fenced context lease and re-read canonical CMA history. No P0 completion path requires CMA SSE, AppSync or live tokens.

`SubscribeToTask` is a notification stream, not replay. Web loads canonical `GetTask` history before subscribing and reconciles after closure/failure, deduplicating A2A message IDs. A newer resume snapshot refreshes loaded history. Reload/disconnect never cancels; explicit Stop calls A2A cancellation and controller-private cleanup.

External calls and DSQL are not one transaction. Claims and stable logical IDs suppress concurrent/repeated work; uncertain CMA side effects remain held until canonical evidence resolves them. Slack receipts protect ordinary replay but cannot promise exactly-once external delivery after an uncertain API acknowledgment.

## Native OpenAPPA human review

Every intercepted CMA tool approval is proposed to native OpenAPPA using fresh in-memory state. The baseline policy automatically allows declared `web_fetch`/`web_search` and denies undeclared calls. The dedicated [review fixture](../services/openappa/tests/human-review.toml) requires `user-approval` for `web_search`.

1. A native allow or bare deny follows the existing fenced decision path. A single native HITL authority/offer with actual review text becomes a pending review. Unsupported remedies and policy/runtime failures remain `WORKING` holds, with no human override.
2. `cma_input_requests` holds the request ID, review fingerprint/authority and nullable human ruling. The existing `pending` status means initial evaluation without a digest, awaiting user with a digest, or awaiting native resolution with a ruling. Review text uses the existing private temporary-object store; tool arguments come from canonical CMA history.
3. `GetTask` advertises `human-input/v1` tool approval only while that review awaits a ruling, sets `INPUT_REQUIRED`, and supplies exact tool arguments and native policy explanation to authorized surfaces. Clarification from `ask_user` uses a separate request type in the same extension.
4. An authenticated Web or signed/authorized Slack action records the first ruling atomically. A repeated identical ruling is idempotent while active; conflicts, stale IDs, wrong Tasks and unauthorized users are rejected. Surface code cannot confirm a CMA tool.
5. In a fresh native process, the scheduler reconstructs the call, verifies the fingerprint of call identity/arguments, current policy and authority, passes the ruling through the native control hook, and executes the newly reconstructed remedy. Allow requires `Authorized` for the exact same tool/arguments. Deny requires native decline.
6. The scheduler commits final allow/deny under the active Task, unexpired lease and cancellation fence before marking/attempting CMA confirmation. A lost acknowledgment never causes a blind repeat. Changed policy/input invalidates the ruling and rotates the request ID before new review.

Stop removes actionable reviews and uses cancellation-deny cleanup even when policy is unavailable. A ruling racing cancellation cannot authorize a canceled call. Review objects use the existing orphan sweep. No long-lived native process, snapshot database, or new bucket is required.

Only the single HITL authority subset is supported: no multiple-offer orchestration, sanitizers, result mediation or history-dependent policy. See [limitations](known-limitations.md) and the [policy operations note](../knowledge/areas/openappa-cma-policy-state.md).

## Slack baseline

Mentions, DMs/Agent View, owned bound-thread replies, explicit `link <thread UUID>`, Stop and native review Allow/Deny are supported. Identity is derived from verified Slack payloads and resolved through DSQL. Processing state and the receipt reaction follow authorization and target the exact triggering message.

Projection opens one chunk-mode task card when possible, appends canonical response messages and stops at completion or human input. If no stream exists, it posts the canonical answer normally. Source permalinks are included when `PUBLIC_APP_URL` is configured. Failed working-state APIs never resubmit/cancel execution. Old controls are retired after cross-surface settlement. Clarification is shown as text and answered through authenticated Web.

Feedback buttons, shortcut workflows, denial-reason modals, broad active-view context injection and link unfurls are removed. Native start/append/stop primitives remain for future preview work, which is outside this implementation.
