---
type: Runbook
title: "Slack Native Working-State Acceptance"
description: "Verify the deployed Slack receipt, task-card, continuity, approval, Stop, source-link, and recovery behaviors."
tags:
  - slack
  - acceptance-testing
  - managed-agents
  - aws
status: stable
aliases:
  - Slack native acceptance runbook
sources:
  - id: local-architecture
    resource: "../../docs/architecture.md"
    title: "Current application architecture"
  - id: local-projector-tests
    resource: "../../backend/tests/test_slack_projector.py"
    title: "Slack projector regression tests"
  - id: local-agent-input-tests
    resource: "../../backend/tests/test_agent_input.py"
    title: "Agent Input ordering and authorization tests"
  - id: local-slack-setup
    resource: "../../docs/slack-setup.md"
    title: "Slack setup documentation"
  - id: local-deployment-wrapper
    resource: "../../scripts/deploy.sh"
    title: "Deployment wrapper"
generated:
  by: openai-codex/gpt-5
  at: 2026-09-05T13:30:00Z
---

# Slack Native Working-State Acceptance

## Goal

Prove that the installed Slack app and deployed AWS application—not only local mocks—support immediate native working state, exact-message context, task streaming, protected controls, and recovery without duplicate responses.

Use a Slack Developer Program sandbox and a DSQL-mapped human test identity. Do not use workspace or user IDs as credentials. Never print bot tokens, signing secrets, Anthropic credentials, cookies, or exported AWS session keys.

## Preconditions

- Slack Socket Mode is disabled.
- Events and interactivity URLs use current Terraform outputs.
- The current manifest is imported and the app is reinstalled.
- The installed bot token includes `reactions:write` plus the existing Agent View, chat, history, command, and link scopes.
- The bot is invited to the public and private test channels.
- The signed Slack workspace/user identity maps to an authorized DSQL principal.
- AWS profile resolves to the intended account; use `EXPECTED_AWS_ACCOUNT_ID` to
  enforce an optional exact-account assertion.
- Receipt reactions, source links, task cards, streaming, Agent View, approvals, and bound-thread replies are enabled for the sandbox rollout.

## Local and live-service gates

Before deployment, run lint, mypy/TypeScript checks, frontend/backend tests, the frontend build, Lambda artifact build, and Terraform validation. The regular Python suite intentionally skips credentialed integration tests.

Run the opt-in tests separately with temporary AWS credentials and the DSQL endpoint from Terraform:

```bash
export AWS_PROFILE=default
export AWS_SDK_LOAD_CONFIG=1
eval "$(aws configure export-credentials --profile default --format env)"
export DSQL_ENDPOINT="$(terraform -chdir=infra/app output -raw dsql_endpoint)"
RUN_INTEGRATION_TESTS=1 backend/.venv/bin/pytest -q backend/tests/integration/test_real_services.py
```

Do not echo the exported credential variables.

## Deploy gate

Run `./scripts/deploy.sh --plan-only` first and inspect its saved plan for core deletion or replacement. Then use `./scripts/deploy.sh`. Confirm the identity check names the expected account and region. The private A2A REST API, endpoint, VPC, and NAT are additive; the existing public ingress routes remain in place.

After apply, confirm:

- The Slack projector is active and its update succeeded.
- Its timeout accommodates a managed turn and bounded recovery.
- All selected feature gates are `true`.
- The CloudFront application returns HTTP 200.
- The EventBridge target DLQ is empty.
- Terraform's `private_a2a_url` rejects requests from outside its VPC. An in-VPC A2A caller obtains the agent card, subscribes to Task updates, and receives push-driven projection.

## Acceptance cases

### Channel mention

Send a unique deterministic request in a public test channel by mentioning the bot. Verify the exact source message receives `:eyes:`, one task-card response appears in its thread, the deterministic marker is present, the task completes, and “Original message” links to that source.

For the Phase 4 cutover, this message must come from the mapped human Slack account. Bot posts and signed synthetic events cannot prove Slack delivered a human event to AWS. Trace its Slack event ID through ingress and input, then confirm one `agent_threads` row, one Slack `thread_surface_bindings` row, one A2A Task, a push receipt, one completed projection, and no new legacy `agent_sessions` row. Use `scripts/live_a2a_inspect.py` for Task metadata only.

### Bound-thread continuity

Reply to the successful channel thread without mentioning the bot. Verify the new nested source receives `:eyes:`. Verify one new Task on the same application thread, one response, and an original-message link containing the nested message timestamp.

### DM and Agent View

Send a new top-level DM with a unique marker. Verify it creates or binds an application thread, receives `:eyes:`, renders one native task card, completes, and links back to the DM source. A later DM thread reply should reuse the binding.

### Tool approval

Use a harmless, explicit prompt for a known approval-gated tool. Before clicking Allow, inspect the canonical Managed Agent tool event and independently verify its name and complete input. Do not approve a tool with uncertain targets or side effects.

Verify the Task is suspended and Slack shows Allow, Deny, and Deny-with-reason actions with the A2A `taskId` and human-input `requestId`. After Allow, the interaction endpoint must acknowledge with HTTP 200, replace the controls with the recorded decision, resume the same Task, and eventually show its final response. Repeat the interaction to confirm an already-resolved result. Test a separate denial with a reason; the reason must survive scheduler retry in the object store without being persisted as DSQL text.

### Native Stop

Create a fresh turn that reaches a pending approval without approving the tool, then trigger Slack's native Stop. Verify Agent Input authorizes the binding and records `agent_stop_requested`. The earliest active A2A Task must become `CANCELED`; a completed Task must not be cancelled. In canonical CMA session events, require `user.interrupt`. If a tool result exists, confirm it is an interruption error rather than successful execution.

### Permalink failure

Exercise `chat.getPermalink` with a known-invalid test timestamp. The Slack API should report a missing message, while the application logs a privacy-safe warning and returns no link without raising out of the projection flow.

### Forced chunk recovery

Start a task-card stream, then finalize the existing stream with a `markdown_text` response chunk and a completed `task_update` chunk. Verify Slack accepts the payload, the marker is visible, the source is linked, the task completes, and no second response message is posted.

Repository tests additionally cover the DSQL recovery claim and receipt transaction. Avoid mutating unrelated historical stream rows merely to reproduce recovery.

## Evidence and failure interpretation

Use unique markers and record only non-sensitive evidence: HTTP status, reaction name, response count, task state, block types, source-link match, Managed Agent event types, Lambda request outcomes, and DLQ counts. Do not retain message bodies in DSQL or diagnostic logs.

A push notification can defer while a valid live subscription lease is active. This is healthy only if the live projector completes, the retry later succeeds idempotently, the user sees one final response, and the DLQ remains empty. The private REST stream has a five-minute idle limit. Subscription failure must mark fallback and recover current Task output through `GetTask`. `message_not_in_streaming_state`, duplicate final replies, a task stuck in progress, or a 405 from the interactions URL are failures.

## Verified sandbox result

On 2026-09-05, the deployed sandbox passed the channel mention, bound-thread reply, DM/Agent View, tool Allow, native Stop, permalink-failure, and chunk-recovery cases. The exact source messages received `eyes`, task cards completed or suspended appropriately, source links matched the triggering messages, live DSQL and Anthropic integration tests passed, and the application DLQ was empty after acceptance.

On 2026-09-27, the dev stack gained a private API Gateway endpoint and the
Phase 4 Slack A2A path. Signed-ingress live tests passed duplicate delivery,
bound replies, Allow, denial reason, active-only Stop, push projection, and an
independent direct-CMA Web turn. A separate guarded bot fixture forced a live
subscription failure after the task card started. Push recovery fetched the
completed Task and finished the same card with one response. The mapped human
account then sent marker `HUMAN_A2A_20260927_P4B7` through Slack. Its event
`Ev0C4MURC28M` yielded one application thread, one completed A2A Task, two push
receipts, one completed projection, and zero legacy session rows. Slack showed
`:eyes:` on the human source and exactly one bot response with the marker. The
four DLQs were empty after acceptance.

## Related knowledge

- [Slack native agent projection](../areas/slack-native-agent-projection.md)
- [Slack Events API operations](../areas/slack-events-api-operations.md)
- [Anthropic Managed Agents operations](../areas/anthropic-managed-agents-operations.md)
- [Managed Agent application end-to-end runbook](managed-agent-application-e2e-runbook.md)
