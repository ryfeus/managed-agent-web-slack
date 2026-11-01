# Architecture

Web and Slack operate on application threads and A2A Tasks. Only the private controller mutates CMA sessions/events and owns canonical reconciliation. A separate disposable preview worker may read CMA SSE. Anthropic owns canonical transcripts and execution. DSQL stores identity, authorization, routing, idempotency, leases, projection state and opaque temporary-object keys; never message bodies, tool arguments/output or reasoning.

## Boundaries

- `apps/web` uses assistant-ui and AG-UI. The cookie-authenticated Node 24 `apps/web-function` HTTP server authorizes every thread operation and translates official A2A HTTP+JSON history/status into standard AG-UI SSE. No AppSync SDK or credentials reach the browser.
- `agent_control_plane` owns application threads, principal ownership, surface bindings and logical input IDs. Slack uses the Python service; Web ports the minimum fenced thread/binding operations in TypeScript over the same tables and constraints. Both use official A2A SDKs to reach the same private controller.
- `cma_controller` owns private provider identity, FIFO Tasks, canonical history reconciliation, native policy and confirmation fencing. Provider IDs never appear in browser payloads.
- Signed Slack ingress hands work to EventBridge/SQS. A2A push callbacks enqueue projection notifications; the short Slack projector reads canonical `GetTask` inside a lease and deduplicates stable message IDs. It never tails CMA or subscribes to a Task.
- Production uses private A2A HTTP+JSON routes and an exact authenticated push destination. Runtime composition constructs adapters; handlers depend on ports.

## Durable execution and recovery

The earliest nonterminal Task is active. Send, history, resume, pending input and Stop select that same FIFO Task. Verified Anthropic webhooks wake the queued scheduler; a 30-second reconciliation schedule recovers missed notifications. Scheduler workers hold a fenced context lease and re-read canonical CMA history. No P0 completion path requires CMA SSE, AppSync or live tokens.

`SubscribeToTask` is a notification stream, not replay. Web loads canonical `GetTask` history before subscribing and reconciles after closure/failure, deduplicating A2A message IDs. A newer resume snapshot refreshes loaded history. Reload/disconnect never cancels; explicit Stop calls A2A cancellation and controller-private cleanup.

External calls and DSQL are not one transaction. Claims and stable logical IDs suppress concurrent/repeated work; uncertain CMA side effects remain held until canonical evidence resolves them. Slack receipts protect ordinary replay but cannot promise exactly-once external delivery after an uncertain API acknowledgment.

## Optional preview and Web deployment

After committing a bound input or resolved human response, the scheduler releases its lease, sends the existing P0 push wakeup, and makes a bounded best-effort FIFO hint. Hints contain private session identity only inside the private queue. The independent Node 24/Arm64 Event Function observes `sessions.events.stream(event_deltas: ['agent.message'])`; it cannot send, confirm or write Task state. It exits on idle/requires_action, cancellation, lost eligibility, error or a 120-second deadline. FIFO grouping by context and transition-only deduplication avoid one reader per viewer. Stale hints expire; they are never replayed as durable work.

The reader coalesces sanitized assistant text every 100 ms into a versioned bounded contract on `/preview/{taskId}`. IAM grants it only namespace publish, while the Web Function can connect and subscribe. Browser ownership and Task binding are verified before attaching. Preview IDs are hashes, offsets suppress duplicates and invalidate gaps, and a final canonical MessagesSnapshot replaces provisional text before A2A-derived completion. Preview end markers never finish a run or authorize a tool. Bodies are neither logged nor persisted.

The Web Function listens on `AWS_LAMBDA_HTTP_ENDPOINT` with Hono and @hono/node-server; it has no Lambda handler or Web Adapter. It uses the official DSQL postgres.js connector and only the Web authentication secrets. CloudFront sends uncached `/api/*` with cookies, excluding viewer Host, through regional SigV4 OAC (`always-amz-auth`) to an `IamAuth` HomeRegion endpoint. `/slack/*` and `/anthropic/*` remain on API Gateway. Terraform owns a compact SAM stack for the Web Function, versioned connector and OAC, plus a separate post-distribution resource-policy stack. Two private subnets in different AZs share the existing NAT; this is not multi-AZ NAT availability.

SSE heartbeats run every 15 seconds. A five-second write timeout bounds slow clients; subscriptions and canonical watches abort on disconnect/shutdown. At 840 seconds the gateway sends recoverable transport termination and the browser reloads canonical history/resumes. No disconnect or preview failure cancels durable execution. See [cutover, costs and rollback](phase-5-rollout.md).

The optional `apps/slack-preview` worker independently subscribes to `/preview/{taskId}`. After opening a new active card and releasing its short claim, the Python projector sends `{v:1,bindingId,taskId,createdAt}` to a separate encrypted FIFO queue. Grouping by binding, deduplication per card generation and rejection of redeliveries/stale hints prevent repeated subscriptions. The destination is resolved by joining trusted projection, Slack surface binding and Task binding records; queue content cannot name a channel or message.

Every two seconds the subscriber checks eligibility and claims `slack_task_projections` for one title-only `chat.appendStream`. The shared `streaming`/claim token has an eight-second lease; DB calls have 1.5-second deadlines and Slack HTTP has a three-second deadline. Only the owning token can release it. P1 never steals an expired claim, changes P0 attempts/receipts, opens/stops a stream or calls A2A. P0 retries a busy row normally, then replaces the draft title before stopping the card at every settled boundary, including human input. There is no schema change, preview ledger or preview body persistence.

The title contains a sanitized last-140-character excerpt, with at most forty updates in 120 seconds. Duplicate titles are suppressed. Outages, rate limits, ambiguous acknowledgments, lost claims, missing streams and approaching Slack's ten-minute stream lifetime stop optional work. The 130-second Event Function has concurrency four, reads only the Slack bot secret, connects to DSQL and connects/subscribes to AppSync. It has no provider credential, publish permission or execution port. All canonical Markdown, controls and execution stay on P0.

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

Feedback buttons, shortcut workflows, denial-reason modals, broad active-view context injection and link unfurls are removed. Phase 6 adds only optional rolling draft titles on existing cards. It does not reopen a settled card after review; resumed output uses the existing canonical reply path.
