---
type: Runbook
title: "Anthropic Managed Agents Operations"
description: "Operate Managed Agent sessions, event streams, signed webhooks, and downstream projections without duplicating the canonical transcript."
tags:
  - anthropic
  - managed-agents
  - webhooks
  - event-streaming
status: stable
aliases:
  - Claude Managed Agents operations
sources:
  - id: anthropic-sessions
    resource: "https://platform.claude.com/docs/en/managed-agents/sessions"
    title: "Start a session"
  - id: anthropic-event-stream
    resource: "https://platform.claude.com/docs/en/managed-agents/events-and-streaming"
    title: "Session event stream"
  - id: anthropic-webhooks
    resource: "https://platform.claude.com/docs/en/managed-agents/webhooks"
    title: "Subscribe to webhooks"
  - id: local-managed-agent-client
    resource: "../../backend/src/managed_agents_app/managed_agent/client.py"
    title: "Application Managed Agent client"
generated:
  by: openai-codex/gpt-5
  at: 2026-09-03T04:34:14Z
---

# Anthropic Managed Agents Operations

## Ownership boundary

Treat the Managed Agent session event log as the canonical source for conversation messages, agent output, tool activity, execution status, and usage. Application storage may authorize a session and bind it to external surfaces, but it must not become a second transcript database.

A session is an instance of an agent in an environment. The agent ID selects behavior and tools; the environment ID selects its execution environment. Load both from deployment configuration.

## Session lifecycle

A session can be created first and started later with a `user.message`, or created with initial events. Initial events reduce round trips and give Slack ingestion a single idempotent creation operation.

Use a stable creation request ID when the API supports it. For Slack, the signed Slack event ID is the natural key. Before creating a replacement session after an uncertain response, search by that creation request ID.

Record only the resulting session ID, its authorized principal, its agent/environment identity, and its originating surface in DSQL.

## Event submission and streaming

Managed Agents communication is event-based. Submit user events and consume session, agent, span, and status events. The SDK supplies the Managed Agents beta header; direct HTTP clients must track the currently required API header from official documentation.

The browser surface should stream canonical events through SSE after checking DSQL session ownership. Stop the stream after a terminal or idle status appropriate to the UI, while allowing the client to reconnect and fetch retained events.

For Slack, do not keep the ingress request open. Submit asynchronously, then use a later webhook to trigger projection.

## Webhooks are change notifications

Register a public HTTPS endpoint in **Manage → Webhooks** in the Claude Console. Store the one-time `whsec_` signing secret in Secrets Manager; never put it in Terraform state or logs.

This application subscribes to:

- `session.status_idled`
- `session.status_terminated`
- `session.budget_reached`

Every delivery includes webhook identity, timestamp, and signature headers. Verify and parse the raw delivery with the Anthropic SDK before emitting an internal event. Reject stale or invalid deliveries.

Webhook payloads identify the changed resource but are not the durable event log. After verification, retrieve the session and list canonical events before deciding what to project. Reconciliation protects against retries, reordering, and missed webhook deliveries.

## Projection rules

When a session changes:

1. Find authorized external surface bindings by session ID.
2. Fetch canonical Managed Agent events.
3. Select projectable agent messages.
4. Claim each `(surface binding, managed event ID)` receipt in DSQL.
5. Post the message to the external surface.
6. Record the external message timestamp.

Use leases for abandoned projection claims so a crashed worker does not suppress a response permanently. Never infer completion from webhook delivery alone.

## Failure handling

- **Session creation timed out:** query by creation request ID before retrying creation.
- **Message submission returned an uncertain result:** retain the client request ID and reconcile the session event log.
- **SSE disconnected:** reconnect and list retained events; do not synthesize missing transcript state locally.
- **Webhook returns 503:** confirm the signing key has an active Secrets Manager version and the Lambda region is correct.
- **Webhook verifies but projection is empty:** retrieve the session events and confirm a new `agent.message` exists.
- **Repeated Slack output:** inspect projection receipt uniqueness and lease recovery.
- **Budget reached:** surface a terminal control message and require an explicit new session or budget decision.

## Deterministic verification

Create a temporary authorized session and send a low-cost deterministic request such as `Reply with exactly: managed-agent-smoke-ok`. Success requires:

- Session creation succeeds.
- The user event is accepted.
- The canonical log contains `session.status_running`, an `agent.message`, and an idle or terminal status.
- The response text matches the assertion.
- A signed webhook for that session is accepted.

Archive temporary web-only smoke sessions. Do not archive user-created Slack sessions automatically because the user may continue them.

## Related knowledge

- [Slack Events API operations](slack-events-api-operations.md)
- [Slack native agent projection](slack-native-agent-projection.md)
- [Aurora DSQL IAM operations](aurora-dsql-iam-operations.md)
- [Managed Agent application end-to-end runbook](../projects/managed-agent-application-e2e-runbook.md)
