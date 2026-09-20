---
type: Runbook
title: "Managed Agent Application End-to-End Runbook"
description: "Deploy and verify the repository's web-to-Claude and Slack-to-Claude-to-Slack paths."
tags:
  - managed-agents
  - slack
  - aws
  - operations
status: stable
aliases:
  - Managed agents deployment runbook
sources:
  - id: repository-agent-context
    resource: "../../AGENTS.md"
    title: "Repository Agent Context"
  - id: repository-architecture
    resource: "../../docs/architecture.md"
    title: "Application architecture"
  - id: repository-deploy-script
    resource: "../../scripts/deploy.sh"
    title: "Deployment wrapper"
  - id: eventbridge-dlq
    resource: "https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-rule-dlq.html"
    title: "Using dead-letter queues to process undelivered events in EventBridge"
generated:
  by: openai-codex/gpt-5
  at: 2026-09-03T04:34:14Z
---

# Managed Agent Application End-to-End Runbook

## Objective

Prove that the deployed system preserves one authorization model and the canonical Managed Agent transcript across two surfaces:

```text
Browser → Web API → Managed Agent → SSE → Browser
Slack → signed HTTP ingress → DSQL authorization → Managed Agent
Managed Agent → signed webhook → EventBridge → Slack projector → Slack thread
```

DSQL stores authorization and routing metadata only.

## Preconditions

- AWS SSO profile is selected through `AWS_PROFILE`; STS resolves the target
  account and optional `EXPECTED_AWS_ACCOUNT_ID` asserts it.
- Deployment uses the supported `AWS_REGION` (default `us-west-2`).
- `.env` contains the required Anthropic, Slack, and web secrets without committing them.
- The Managed Agent and environment exist.
- The Slack app is installed or reinstalled from the generated Slack manifest in a
  Developer Program sandbox.
- Slack Socket Mode is disabled.
- Slack Event Subscriptions use the Terraform `slack_events_url` and include `app_mention`.
- The Anthropic webhook uses the Terraform `anthropic_webhook_url` and its signing key has been synced.

## Local validation

Run before changing AWS:

```bash
npm run lint
npm run typecheck
npm test
npm run build
npm run terraform:validate
```

Do not deploy when any check fails. Packaging may require execution outside a restrictive local sandbox because the TypeScript runner creates an IPC socket.

## AWS credentials

```bash
aws sso login --profile default
aws sts get-caller-identity --profile default
```

Confirm the discovered account is the intended target (and matches
`EXPECTED_AWS_ACCOUNT_ID` when configured). For direct Terraform or SDK commands,
export concrete credentials so they use the same SSO session:

```bash
export AWS_PROFILE=default
export AWS_SDK_LOAD_CONFIG=1
export AWS_REGION=us-west-2
export AWS_DEFAULT_REGION=us-west-2
eval "$(aws configure export-credentials --profile default --format env)"
```

Never print the exported values.

## Deploy

Use the wrapper:

```bash
./scripts/deploy.sh
```

It validates the account, builds the application, initializes remote state, applies Terraform, syncs secrets, migrates/seeds DSQL, uploads static assets, and invalidates CloudFront.

Review the Terraform plan before accepting unexpected replacement or deletion. A Lambda-only code update should normally show an in-place update.

## Configure callbacks

### Slack

1. Disable Socket Mode.
2. Enable Event Subscriptions.
3. Set the verified Events Request URL from `terraform -chdir=infra/app output -raw slack_events_url`.
4. Set the Interaction Request URL from `terraform -chdir=infra/app output -raw slack_interactions_url`.
5. Render the Slack manifest with `npm run slack:manifest`, import it, and invite
   the app to the public/private test channels.

### Anthropic

1. Open **Manage → Webhooks** in the Claude Console.
2. Add the Terraform `anthropic_webhook_url`.
3. Subscribe to the configured session status events.
4. Store the one-time signing key in local `.env` as `ANTHROPIC_WEBHOOK_SIGNING_KEY`.
5. Run `npm run secrets:sync` with concrete AWS credentials.

## Bootstrap Slack identity safely

1. Have the human user mention the bot in a channel.
2. Confirm `slack-ingress` logs `slack_event_accepted`.
3. Confirm `agent-input` logs `slack_identity_unmapped` with signed `team_id` and `user_id`.
4. Set the DSQL endpoint from Terraform.
5. Map the verified pair:

   ```bash
   export DSQL_ENDPOINT="$(terraform -chdir=infra/app output -raw dsql_endpoint)"
   npm run db:map-slack -- --team-id T01234567 --user-id U01234567
   ```

6. Ask the user to send a new mention. Do not replay the completed unmapped event.

Do not add the IDs to `.env` unless a temporary development traffic filter is explicitly desired.

## Web smoke test

Use the web access token to establish a signed cookie, then:

1. Create a session with a unique client request UUID.
2. Send `Reply with exactly: managed-agent-smoke-ok` with another UUID.
3. Read the session events.
4. Verify running status, user message, agent message, usage, and idle status.
5. Verify the exact response text.
6. Verify `anthropic-webhook` accepted a signed idle event for the same session.
7. Archive the temporary session.

Never print the access token or signed cookie.

## Slack smoke test

Send this from the mapped human identity in a channel containing the bot:

```text
@bot Reply with exactly: slack-smoke-ok
```

Success requires correlated evidence:

1. `slack-ingress`: `slack_event_accepted`.
2. `agent-input`: `slack_message_submitted` with the mapped principal and a session ID.
3. `anthropic-webhook`: `anthropic_webhook_accepted` for that session.
4. `slack-projector`: `slack_message_projected` for a Managed Agent event ID.
5. The Slack thread contains `slack-smoke-ok`.
6. Reading that session through the authorized web principal shows the same canonical agent message.

Keep Slack smoke sessions unless the user asks to archive them; they demonstrate cross-surface continuation.

## Failure isolation

Follow the path in order:

| Symptom | First check |
| --- | --- |
| Slack mention never reaches Lambda | Socket Mode, Event Subscriptions, Request URL, `app_mention` |
| Ingress ignores the request | Envelope/event type, subtype, bot ID, required field presence |
| Identity is unmapped | Signed team/user pair and DSQL external identity |
| Agent input fails | DSQL role, session creation idempotency, Anthropic credentials |
| No completion callback | Anthropic subscription, signing key, webhook Lambda logs |
| No Slack response | Surface binding, projection receipt, bot scope, channel membership |
| EventBridge delivery failure | Rule metrics and the standard SQS target DLQ |
| Terraform works in CLI but Python fails | Concrete credentials and explicit AWS region |

EventBridge target DLQ messages include rule, target, retry, and error metadata. Inspect without deleting first; resolve the cause before replaying. Preserve idempotency keys during any replay.

## Current verified result

On 2026-09-02 local time, the deployed development environment completed both deterministic tests. The Slack identity came from signed HTTP events and was mapped in DSQL; no Slack allowlist value was used as authentication.

## Related knowledge

- [Slack Events API operations](../areas/slack-events-api-operations.md)
- [Slack native agent projection](../areas/slack-native-agent-projection.md)
- [Anthropic Managed Agents operations](../areas/anthropic-managed-agents-operations.md)
- [Aurora DSQL IAM operations](../areas/aurora-dsql-iam-operations.md)
- [Slack native working-state acceptance](slack-native-working-state-acceptance.md)
