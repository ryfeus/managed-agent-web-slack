# Feature Contract — Web AG-UI to private A2A

## Goal

Web and Slack use the same application thread, A2A context, controller Tasks, and canonical provider-owned history. The browser speaks AG-UI to a cookie-authenticated bridge and never sees CMA identifiers or the private A2A endpoint.

## Non-goals

Migrating legacy Web sessions, persisting transcripts in DSQL, completed provider tool traces, attachments, and real-time Web projection of Slack-initiated turns into an already-open tab.

## User-visible behavior

Conversations have shareable `?thread=<uuid>` links. Runs stream final agent messages, reload restores canonical history and active work, pending tool approvals and clarifications can be answered, and explicit Stop cancels the active A2A Task. A Web turn on a Slack-bound thread projects to Slack.

## Inputs

An authenticated AG-UI `RunAgentInput` identifies an application `threadId` and client-generated `runId`. Resume entries identify every current interrupt. Browser transcripts are never execution authority.

## Outputs / side effects

Ordinary input creates one logical `web:agui:<runId>` send and one A2A Task. Human input continues that Task. History and stream events are derived from A2A Tasks and provider history. DSQL records only identity, ownership, routing, idempotency, and projection metadata.

## Invariants

- AG-UI `runId` never becomes the A2A Task ID.
- The application-level active Task is the earliest nonterminal Task in controller FIFO order, including when a later Task is queued.
- Only the controller imports CMA execution adapters. The bridge uses `ThreadAgentService` and provider-neutral `human-input/v1`.
- Cancellation requires the explicit Stop endpoint; disconnect, reload, and thread switch leave durable work running.
- Provider event IDs, message bodies, answers, and tool arguments do not enter DSQL.

## Failure semantics

- Before admission, validation or ownership failure returns HTTP 4xx.
- After ambiguous admission, the same `runId` retries the same logical send and controller Task.
- A lost stream is recovered from `GetTask` and canonical history. Subscription closure is not proof of completion.
- A bridge failure after stream start emits a safe AG-UI error when possible; a dead process leaves the Task running for reload recovery.

## Concurrency / idempotency semantics

- `(principal_id, creation_request_id)` identifies one created thread.
- `(thread_id, client_message_id)` identifies one ordinary send.
- Stable A2A message IDs reconcile history with resume snapshots and deduplicate live subscription updates. A newer snapshot refreshes canonical browser history.
- Human-input response IDs derive from run and interrupt IDs. Every currently open interrupt must have one explicit response.

## Security / authorization constraints

The bridge validates the existing HttpOnly cookie and checks `agent_threads.principal_id` before run, history, resume, or cancellation. Unknown and foreign thread IDs yield the same not-found response. A2A remains private and is never configured in browser JavaScript.

## Persistence constraints

Clarification answers and denial reasons use controller-owned temporary object storage; DSQL holds opaque keys only until reconciliation. Migration-owned metadata columns and tables appear in `knowledge/persistence-schema-contract.json`.
The thread-creation idempotency index builds asynchronously on DSQL and must be valid before migration 007 is recorded.

## Compatibility constraints

The Web session API and stream are retired in this phase. Old rows remain for Phase 6 cleanup. The pinned assistant-ui AG-UI adapter accepts success and interrupt outcomes but not Python AG-UI 1.0's cancelled outcome, so a remotely cancelled Task emits a safe `RUN_ERROR`. Explicit Stop still cancels the A2A Task and local run.

## Verification mapping

| Requirement | Test / verifier |
|---|---|
| Auth, ownership, retry-safe thread creation | Web API unit and Postgres integration |
| AG-UI mapping, idempotency, recovery, history/resume race | Bridge unit, Postgres integration, and deterministic Playwright barrier |
| FIFO active Task and queued pending input | Service and history unit tests, Playwright queued-Task reload |
| Interrupts, clarification, Stop while working or input-required, reload, cross-surface | Playwright local A2A E2E |
| Exact Phase 5 Terraform retirement policy | Destructive-plan guard fixture and real plan-only review |
| No provider imports or transcript storage | Architecture and persistence rails |
| Build, types, formatting, Terraform | `./scripts/verify --full` |

## Live-contract requirements

The deployed public streaming integration, real CMA turn and approval, explicit Stop, and linked Slack projection need live AWS acceptance. A configured `ask_user` custom tool is required to prove real clarification behavior.

On 2026-09-27, the `dev` deployment in the developer sandbox AWS account passed the live Slack/Web suite twice. The second run covered Web reload during a nonterminal Task, approval after reload, Stop during pending approval, one Task per Web run, linked Slack projection, and no legacy Web session creation. The A2A API is `PRIVATE` and restricted to its VPC endpoint; relevant dead letter queues were empty after both runs. Real clarification remains unverified because the deployed agent has no configured `ask_user` custom tool.

## Known limitations

The bridge must recover after the early subscription closure tracked as KL-004.

## Completion checklist

- [x] Local verification passes.
- [x] Production-built browser E2E passes.
- [x] Live AWS and Slack acceptance passes.
