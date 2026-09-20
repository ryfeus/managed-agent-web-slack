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

Use `./scripts/deploy.sh`. Confirm the identity check names the expected account and region. Review the Terraform plan; Lambda code/configuration changes should be in-place, and unexpected replacements or deletions require investigation.

After apply, confirm:

- The Slack projector is active and its update succeeded.
- Its timeout accommodates a managed turn and bounded recovery.
- All selected feature gates are `true`.
- The CloudFront application returns HTTP 200.
- The EventBridge target DLQ is empty.

## Acceptance cases

### Channel mention

Send a unique deterministic request in a public test channel by mentioning the bot. Verify the exact source message receives `:eyes:`, one task-card response appears in its thread, the deterministic marker is present, the task completes, and “Original message” links to that source.

### Bound-thread continuity

Reply to the successful channel thread without mentioning the bot. Verify the new nested source—not the root—receives `:eyes:`. Verify one new response, a completed task, and an original-message link containing the nested message timestamp.

### DM and Agent View

Send a new top-level DM with a unique marker. Verify it creates or binds a session, receives `:eyes:`, renders one native task card, completes, and links back to the DM source. A later DM thread reply should reuse the binding.

### Tool approval

Use a harmless, explicit prompt for a known approval-gated tool. Before clicking Allow, inspect the canonical Managed Agent tool event and independently verify its name and complete input. Do not approve a tool with uncertain targets or side effects.

Verify the task is suspended and Slack shows Allow, Deny, and Deny-with-reason actions. After Allow, the interaction endpoint must acknowledge with HTTP 200, replace the controls with the recorded decision, resume processing, and eventually show the final response and a completed task. Reusing the same interaction body must be idempotent.

### Native Stop

Create a fresh turn that reaches a pending approval without approving the tool, then trigger Slack's native Stop. Verify Agent Input authorizes the binding and records `agent_stop_requested`. In the canonical session events, require `user.interrupt`. If a tool result exists, confirm it is an interruption error rather than successful execution.

### Permalink failure

Exercise `chat.getPermalink` with a known-invalid test timestamp. The Slack API should report a missing message, while the application logs a privacy-safe warning and returns no link without raising out of the projection flow.

### Forced chunk recovery

Start a task-card stream, then finalize the existing stream with a `markdown_text` response chunk and a completed `task_update` chunk. Verify Slack accepts the payload, the marker is visible, the source is linked, the task completes, and no second response message is posted.

Repository tests additionally cover the DSQL recovery claim and receipt transaction. Avoid mutating unrelated historical stream rows merely to reproduce recovery.

## Evidence and failure interpretation

Use unique markers and record only non-sensitive evidence: HTTP status, reaction name, response count, task state, block types, source-link match, Managed Agent event types, Lambda request outcomes, and DLQ counts. Do not retain message bodies in DSQL or diagnostic logs.

A webhook invocation can log an expected retry while a valid live-stream lease is active. This is healthy only if the live projector completes, the retry later succeeds idempotently, the user sees one final response, and the DLQ remains empty. `message_not_in_streaming_state`, duplicate final replies, a task stuck in progress, or a 405 from the interactions URL are failures.

## Verified sandbox result

On 2026-09-05, the deployed sandbox passed the channel mention, bound-thread reply, DM/Agent View, tool Allow, native Stop, permalink-failure, and chunk-recovery cases. The exact source messages received `eyes`, task cards completed or suspended appropriately, source links matched the triggering messages, live DSQL and Anthropic integration tests passed, and the application DLQ was empty after acceptance.

## Related knowledge

- [Slack native agent projection](../areas/slack-native-agent-projection.md)
- [Slack Events API operations](../areas/slack-events-api-operations.md)
- [Anthropic Managed Agents operations](../areas/anthropic-managed-agents-operations.md)
- [Managed Agent application end-to-end runbook](managed-agent-application-e2e-runbook.md)
