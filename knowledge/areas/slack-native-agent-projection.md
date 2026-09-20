---
type: Topic
title: "Slack Native Agent Projection"
description: "Project Managed Agent work into Slack processing status, receipts, source-linked task cards, approvals, and race-safe completion."
tags:
  - slack
  - agent-view
  - streaming
  - task-cards
  - eventbridge
status: stable
aliases:
  - Slack task-card streaming
  - Slack agent working state
sources:
  - id: slack-streaming-api
    resource: "https://docs.slack.dev/reference/methods/chat.startStream/"
    title: "chat.startStream method"
  - id: slack-reactions-api
    resource: "https://docs.slack.dev/reference/methods/reactions.add/"
    title: "reactions.add method"
  - id: slack-agent-sessions
    resource: "https://docs.slack.dev/ai/agent-sessions/"
    title: "Slack agent sessions"
  - id: local-slack-projector
    resource: "../../backend/src/managed_agents_app/handlers/slack_projector.py"
    title: "Application Slack projector"
  - id: local-agent-input
    resource: "../../backend/src/managed_agents_app/handlers/agent_input.py"
    title: "Application Agent Input handler"
  - id: local-architecture
    resource: "../../docs/architecture.md"
    title: "Current application architecture"
generated:
  by: openai-codex/gpt-5
  at: 2026-09-05T13:30:00Z
---

# Slack Native Agent Projection

## Boundary and event order

Slack working state is a projection of an authorized Managed Agent turn, not execution state owned by Slack. Anthropic remains the canonical transcript and execution system. DSQL stores identity, ownership, bindings, idempotency, leases, and projection receipts without Slack or Claude message bodies.

The input path must establish trust before visible work begins:

1. Claim the signed Slack event or interaction ID.
2. Resolve the signed workspace/user identity through DSQL.
3. Validate command or message text.
4. If a binding exists, verify that the resolved principal owns its Managed Agent session.
5. Emit `SlackWorkStarted`.
6. Deliver the input to the Managed Agent.
7. Emit `SlackProjectionRequested` with the exact triggering message timestamp.

An unauthorized binding receives a generic rejection and a completed ingress receipt. It must emit neither working state nor Claude input.

## Immediate working state

`SlackWorkStarted` is handled asynchronously by the Slack projector. When enabled, it sets the agent session to `processing` and adds the configured receipt reaction to the exact triggering message. The current sandbox uses `eyes`.

Reaction delivery is idempotent: Slack's `already_reacted` result is success. Other Slack failures should retry through EventBridge and its DLQ without rolling back or suppressing the authorized Managed Agent input.

`reactions:write` must be present in the manifest and in the installed bot token. Importing a changed manifest is not enough; reinstall the application, then prove the scope with a successful `reactions.add` call.

## Source identity and links

Keep two timestamps:

- `threadTs` identifies the conversation binding and response thread.
- `sourceMessageTs` identifies the exact message that triggered the turn.

Use `sourceMessageTs` for the receipt reaction and `chat.getPermalink`. This distinction matters for unmentioned replies inside an already-bound thread: the source is the nested reply, not the root mention.

Permalink lookup is useful context, not a condition for execution. Log a privacy-safe warning and continue without the link when Slack returns an error such as `message_not_found`.

## One streaming mode per Slack message

Slack requires one streaming mode for a message's full lifetime.

Task-card mode:

1. Call `chat.startStream` immediately with a `task_update` chunk, timeline display, and optional “Original message” URL source.
2. Append model output using `markdown_text` chunks through `chat.appendStream`.
3. Call `chat.stopStream` with a completed task chunk only when no approval is pending.
4. If approval is pending, omit the completed chunk and stop with session status `suspended`.

Text mode starts with the `markdown_text` request field, appends text, and may attach the source permalink as a final context block. Never mix text mode and chunk mode on one Slack message. Never send thinking or chain-of-thought through either mode.

Task cards depend on streaming. If task cards are enabled while streaming is disabled, log the configuration problem and behave as though task cards are off rather than failing startup.

## Fast-turn and webhook races

A Managed Agent live stream may begin after a fast turn already completed. The subscription is not guaranteed to replay the final message, so blindly waiting on it can hold a Lambda open until timeout.

Use canonical-event reconciliation around the live subscription:

1. Start the Slack task card early.
2. List Managed Agent events and isolate the turn after the submitted input event, or after the latest user message as a compatibility fallback.
3. If that turn already contains an agent message and idle/terminal status, append the final message and close the task without subscribing.
4. Otherwise subscribe with thinking excluded and a bounded network timeout.
5. Break as soon as `agent.message` arrives.
6. After a stream error or timeout, list canonical events again and recover the retained final message or pending-tool state.
7. Append the final agent message when no deltas were emitted.

The webhook projector can observe completion while the live projector is still appending. It must defer while DSQL reports a valid active stream lease. Recovery may claim only a stream explicitly marked as fallback or one whose lease has expired. This prevents the webhook from stopping the Slack stream before a live append, which otherwise produces `message_not_in_streaming_state` and can lead to duplicate fallback replies.

## Approvals and Stop

When the canonical turn is idle with `requires_action`, render Allow/Deny controls only after verifying the session binding and current pending event. An Allow or Deny interaction must repeat identity resolution, ownership verification, and pending-state verification before submitting `user.tool_confirmation`.

After Allow, replace the controls with the decision, set status to `processing`, and open a new projection attempt for resumed execution. A task remains suspended until no approval is pending.

`agent_session_stopped` must also repeat authorization and ownership checks before submitting `user.interrupt`. An interrupted pending tool can appear in the canonical log as an error tool result stating that execution was interrupted, followed by `user.interrupt`; this is cancellation evidence, not successful tool execution.

## Related knowledge

- [Slack Events API operations](slack-events-api-operations.md)
- [Anthropic Managed Agents operations](anthropic-managed-agents-operations.md)
- [Managed Agent application end-to-end runbook](../projects/managed-agent-application-e2e-runbook.md)
- [Slack native working-state acceptance](../projects/slack-native-working-state-acceptance.md)
