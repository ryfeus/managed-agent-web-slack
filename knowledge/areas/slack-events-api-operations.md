---
type: Runbook
title: "Slack Events API Operations"
description: "Operate a signed HTTP Slack Events API integration with event-derived identity and DSQL authorization."
tags:
  - slack
  - events-api
  - identity
  - security
status: stable
aliases:
  - Slack HTTP event operations
sources:
  - id: slack-events-api
    resource: "https://api.slack.com/apis/connections/events-api"
    title: "Slack Events API"
  - id: slack-request-signing
    resource: "https://api.slack.com/docs/verifying-requests-from-slack"
    title: "Verifying requests from Slack"
  - id: slack-app-mention
    resource: "https://api.slack.com/events/app_mention"
    title: "app_mention event"
  - id: slack-socket-mode
    resource: "https://api.slack.com/apis/connections/socket"
    title: "Slack Socket Mode"
  - id: local-slack-adapter
    resource: "../../backend/src/managed_agents_app/slack/normalize.py"
    title: "Application Slack adapter"
generated:
  by: openai-codex/gpt-5
  at: 2026-09-03T04:34:14Z
---

# Slack Events API Operations

## Operating model

Use Slack's HTTP Events API for the deployed serverless application. Slack sends a signed request to the public endpoint, the ingress adapter verifies the unmodified body, normalizes an allowed event, emits an internal domain event, and returns HTTP 200 promptly. Downstream work happens asynchronously.

This project consumes Agent View/DM messages, top-level channel `app_mention` events, replies to already-bound public/private channel threads, agent lifecycle/context events, and shared session links. Interactive tool, feedback, linking, and shortcut payloads use a separate signed endpoint.

## HTTP and Socket Mode are alternatives

Slack delivers events through either a public HTTP Request URL or Socket Mode. When Socket Mode is enabled, events go to WebSocket connections and not to the saved HTTP Request URL.

For this architecture:

1. Disable **Socket Mode** in the Slack app settings.
2. Enable **Event Subscriptions**.
3. Set the Request URL to the Terraform `slack_events_url` output.
4. Import the repository's versioned manifest so events, interactivity, shortcuts, scopes, and unfurl domains remain synchronized.
5. Save the configuration and reinstall the app if Slack requests reauthorization.

An existing `xapp-` app-level token does not need to be deleted, but it must not be interpreted as proof that HTTP delivery is active.

## Verify every HTTP request

Verification must happen before JSON normalization:

1. Read the raw request body without reserialization.
2. Read `X-Slack-Request-Timestamp` and reject timestamps outside a five-minute window.
3. Build `v0:{timestamp}:{rawBody}`.
4. Compute HMAC-SHA256 with the Slack signing secret.
5. Prefix the hex digest with `v0=` and compare it to `X-Slack-Signature` using a timing-safe comparison.

The signing secret authenticates Slack as the sender. A bot token authorizes outbound Slack Web API calls; it does not authenticate inbound events.

## Identity and authorization

After signature verification, extract identity from the event envelope:

- Tenant: top-level `team_id`.
- Human identity: `event.user`.
- Conversation: `event.channel` plus `event.thread_ts` or the message `event.ts`.
- Delivery identity: top-level `event_id` for idempotency.

Treat the workspace and user IDs as signed identity claims, not credentials. Resolve the tuple `(provider='slack', team_id, event.user)` through DSQL before creating or accessing a Managed Agent session.

`SLACK_TEAM_ID` and `SLACK_USER_ID` may narrow development traffic, but an allowlist match must never create authorization. An identity still needs a DSQL mapping to an application principal.

## Normalize defensively

Accept only known `event_callback` types. Ignore bot-authored events and message subtypes to prevent reply loops. Require an event ID, team ID, human user ID, channel ID, and message timestamp for message input. A top-level channel message must be an `app_mention`; a plain channel message must be a reply and is processed only if DSQL already has a binding.

Remove the bot mention token from the text before submitting it to the agent. Preserve the original Slack `event_id` as the idempotency key.

Slack does not deliver direct-message content as `app_mention`; Agent View/DM continuity uses `message.im`.

## Idempotency and acknowledgements

Slack retries deliveries when acknowledgements fail or time out. Return HTTP 200 after signature validation and durable handoff to EventBridge. Claim the Slack `event_id` in DSQL before creating a session or sending a user event.

An unmapped identity should receive a safe control reply and be recorded as completed, not retried indefinitely. After an administrator maps the signed identity, the user must send a new mention because the prior event remains completed.

## Troubleshooting ladder

1. **No ingress Lambda invocation:** confirm Socket Mode is disabled, Event Subscriptions are enabled, the Request URL is verified, and `app_mention` is subscribed.
2. **Ingress invoked but event ignored:** inspect privacy-safe metadata for envelope type, nested event type, subtype, bot ID presence, and required-field presence.
3. **`slack_identity_unmapped`:** use the logged signed `team_id` and `user_id` to create a DSQL external identity mapping.
4. **Agent input succeeds but no webhook arrives:** inspect Anthropic endpoint subscriptions and signing-secret synchronization.
5. **Webhook succeeds but no Slack response:** inspect the Slack projector, projection receipts, bot token, channel membership, and `chat:write` scope.
6. **Duplicate responses:** verify the ingress claim and projection receipt keys are unique and atomically recorded.

Never add message text or secret values to diagnostic logs.

## Related knowledge

- [Slack native agent projection](slack-native-agent-projection.md)
- [Anthropic Managed Agents operations](anthropic-managed-agents-operations.md)
- [Aurora DSQL IAM operations](aurora-dsql-iam-operations.md)
- [Managed Agent application end-to-end runbook](../projects/managed-agent-application-e2e-runbook.md)
- [Slack native working-state acceptance](../projects/slack-native-working-state-acceptance.md)
