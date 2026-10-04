# Feature Contract — Slack A2A migration

## Goal

A Slack thread binds to one application thread, whose controller context owns reusable conversation state. Each user turn becomes one A2A Task. Slack input and projection use the application control plane, with no direct Managed Agent execution path.

## Non-goals

Web migration, public or private production A2A ingress activation, legacy table deletion, and Slack clarification-response controls.

## User-visible behavior

Mentions, enabled DMs and bound replies, shortcuts, approvals, stop, feedback, and thread links retain their Slack behavior. Task cards stream complete A2A agent Messages rather than provider text deltas. A Task ending restores active Agent View state without closing the reusable thread.

After a tool approval resumes a suspended Task, the final reply must contain the
answer in the Slack thread as well as its notification. Any non-streamed answer
with feedback or source-link blocks includes visible text sections before those
blocks, each within Slack's 3,000-character section limit. Plain replies without
blocks continue to use top-level text. Streamed replies retain their existing mode.

## Inputs

Signed Slack HTTP events and interactions become normalized domain events. `A2ATaskUpdated` is a metadata-only wake-up; the projector fetches the current Task.

## Outputs / side effects

Slack input creates or reuses an application thread and binding, sends a Task through `ThreadAgentService`, records projection metadata, and publishes `SlackTaskProjectionRequested`. The projector posts agent text, approval controls, task state, and receipt UX through the Slack gateway.

## Invariants

- Slack code never imports CMA/provider implementations or reads provider event IDs.
- The controller receives no Slack team, channel, user, or binding identity.
- DSQL stores identity, authorization, routing, claims, and projection metadata, not conversation text or tool outputs.
- A live subscription is a latency aid; push plus `GetTask` is recovery truth.

## Failure semantics

- Before A2A admission: retry the same Slack-derived client message ID.
- After ambiguous A2A admission: retry the same ID; controller admission deduplicates.
- After a live subscription error: release the stream lease as fallback, then recover from current Task on `A2ATaskUpdated`.
- Projector errors remain retryable through EventBridge and its DLQ. Slack receipt and status errors do not prevent an authorized Task send.

## Concurrency / idempotency semantics

- `(surface, external_event_id)` fences duplicate Slack delivery.
- The thread and surface binding are created in one transaction; ownership conflicts never rebind.
- `(thread_id, client_message_id)` and A2A `messageId` identify one logical send.
- `(binding_id, task_id, item_kind, item_id)` deduplicates agent Messages and human-input requests across live and push paths.
- A push update encountering an active live lease retries after the stream completes or falls back.

## Security / authorization constraints

The edge verifies Slack's timestamped HMAC. Input resolves the human Slack identity to an application principal and checks `agent_threads.principal_id` for every bound-thread operation. Approval and feedback Task IDs must belong to that thread. Shared links must match `PUBLIC_APP_URL` origin and an owned, unarchived thread.

## Persistence constraints

Denial reason text goes to the controller's pending-input object store; DSQL holds only its opaque key until reconciliation. Projection tables contain no A2A Message body or Slack text.

## Compatibility constraints

Web remains legacy/direct-CMA until Phase 5. Thread links and `?thread=` unfurls retain their existing feature gates. Controller-private CMA identifiers never cross the Slack boundary.

## Verification mapping

| Requirement | Test / verifier |
|---|---|
| Input, duplicate, and lost-response idempotency | Postgres Slack A2A integration |
| Live/push race and subscription fallback | Postgres Slack A2A integration |
| Approval, denial reason, and response race | Postgres Slack A2A integration |
| Visible final answer after Allow with streaming on or off; duplicate completion | `test_approval_resume_posts_one_visible_answer` |
| Visible non-streamed answer with feedback/source links and long text | `test_nonstreamed_answer_is_visible_with_optional_blocks` |
| Real Slack mention, exact tool input, signed Allow, visible Slack and Web answer | Opt-in `e2e/live/slack-approval.spec.ts` |
| Stop, shortcuts, feedback, links, and multi-binding | Postgres Slack A2A integration and Playwright Slack suite |
| No provider import or transcript persistence | Architecture and persistence rails |
| Web and controller regression | `./scripts/verify --full` |

## Live-contract requirements

Real Slack and deployed private A2A connectivity remain to be verified during private-control-plane activation. Local ASGI and recording-gateway tests do not establish real-provider compatibility.

Approval acceptance must inspect the final Slack message's visible blocks;
notification fallback text alone cannot establish that the answer is visible.

On 2026-10-03, the focused `slack-approval.spec.ts` passed against the developer
sandbox after deployment. A genuine user-token mention reached a pending
`web_fetch` for `https://example.com`; a signed Allow interaction resumed that same
Task. The final reply appeared once in Slack with `section`, `context_actions`,
and `context` blocks, and once in canonical Web history. Bot-token reads verified
the Slack payload; this run did not exercise the Slack client's button UI.

## Known limitations

None added by this contract.

## Completion checklist

- [x] Integration, Playwright, and full verifier pass.
- [x] No direct CMA Slack path remains.
- [x] Private production ingress remains unactivated until its separate cutover.
