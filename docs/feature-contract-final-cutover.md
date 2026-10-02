# Feature Contract — Final A2A cutover

## Goal

Web and Slack continue one authorized application thread through controller-owned A2A Tasks. CMA session identity, canonical history, and provider execution remain private to the controller.

## Non-goals

New agent types, transcript storage, compatibility with legacy session links, changes to ordinary-turn ordering, or a multi-AZ infrastructure redesign.

## User-visible behavior

Web uses `?thread=<uuid>` links, AG-UI streaming, history reload, human input, and explicit Stop. Slack uses the same thread through signed ingress, task projection, approvals, and Stop. Both surfaces reconcile the same A2A Task.

## Inputs

The browser sends AG-UI `RunAgentInput` with an application thread and run ID. Slack sends signed events and interactions with verified workspace and human user identity. Human input names the controller's `requestId` for an A2A Task.

## Outputs / side effects

`ThreadAgentService` sends or resumes an A2A Task. The controller owns CMA session creation, provider input, event reconciliation, and interruption. Surface projections use A2A history and task state.

## Invariants

- Production Web and Slack code cannot import CMA execution adapters, read provider events, or use CMA session IDs.
- The controller cannot import Web or Slack implementation or store surface identity.
- DSQL stores identity, authorization, routing, idempotency, projection state, controller metadata, and opaque temporary-object keys, never transcripts, reasoning, or tool output.
- The earliest nonterminal Task in controller FIFO order is the active application Task.

## Failure semantics

- A lost subscription or early stream close triggers `GetTask` reconciliation; closure never proves terminal state.
- Ambiguous provider send remains held for operator review under KL-003; it is not automatically retried.
- Slack status, reaction, and permalink projection failures do not prevent an authorized input from running.

## Concurrency / idempotency semantics

- Logical Web and Slack send IDs converge on one controller Task after retry.
- A controller human-input request accepts the first valid response atomically across surfaces. Equivalent retries are idempotent; conflicting retries fail.
- Explicit Stop cancels the selected A2A Task; both surfaces remove actionable input after state reconciliation.

## Security / authorization constraints

The Web bridge verifies its signed cookie and thread ownership. Slack ingress verifies its timestamped HMAC signature, then resolves the verified human identity through `external_identities` and checks existing binding ownership. The A2A API is private to its execute-api VPC endpoint. Only controller/provider components receive the Anthropic API secret.

## Persistence constraints

Migration `008_remove_legacy_application_sessions.sql` removes `slack_response_streams`, `agent_feedback`, `projection_events`, `ingress_events`, `surface_bindings`, and `agent_sessions`. Current application and controller tables remain. Denial reasons and clarification answers live in temporary object storage; DSQL stores opaque keys until resolution or cleanup.

## Compatibility constraints

There is no production `/api/sessions`, `?session=`, or `link sesn_...` fallback. Native Slack `agent_session_stopped` remains an input event but maps to Task cancellation. Provider-side session terms remain valid within the CMA controller.

## Verification mapping

Migration-from-007 Postgres tests prove destructive schema cutover. Architecture, persistence, frontend, and IAM rails prove boundary exclusions. Controller and semantic E2E tests cover retry, FIFO, human input, cancellation, projection, and recovery. `./scripts/verify --full` is the local gate; the reviewed Terraform plan and live AWS/Slack matrix are the deployment gate.

## Live-contract requirements

The dev cutover must prove real Web and mapped-human Slack turns, cross-surface continuity, approval, Stop, private A2A denial outside the VPC, empty relevant DLQs, and absence of legacy tables. Real `ask_user` clarification remains unverified until the deployed agent has the custom tool.

## Known limitations

KL-003 covers ambiguous provider acceptance without authoritative evidence. KL-004 covers the pinned A2A client's early iterator termination; `GetTask` and push reconciliation remain required.

## Completion checklist

- [x] Full local verification passes.
- [x] Reviewed Phase 5→6 Terraform plan and dev deployment pass.
- [x] Migration `008` and live acceptance matrix pass.

The 2026-09-28 dev acceptance included a real mapped-human Slack mention and
bound reply, the deployed Web/Slack approval and cancellation suite, a
cross-surface approval race, an external private-A2A negative check, final DSQL
catalog inspection, deployed IAM inspection, and empty relevant dead letter
queues. Real `ask_user` clarification still requires an agent with that custom
tool and remains unverified.
