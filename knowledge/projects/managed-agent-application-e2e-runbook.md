---
type: Runbook
title: "Managed Agent Application End-to-End Runbook"
description: "Deploy and verify Web and Slack through the shared application thread and private A2A controller."
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

Prove that Web and Slack continue one authorized application thread and A2A context:

```text
Web → AG-UI bridge → ThreadAgentService → private A2A controller → CMA
Slack ingress → ThreadAgentService → private A2A controller → CMA
CMA webhook → controller scheduler → A2A push → application event sink → Slack projector
```

DSQL contains application identity, routing, idempotency, projection, and controller metadata only. Provider sessions and canonical content stay behind the controller.

## Preconditions and local gate

- `AWS_PROFILE` selects the intended account; verify it with STS. Region defaults to `us-west-2`.
- Required Anthropic, Slack, and Web secrets stay in untracked `.env`.
- The Slack app is installed from the generated manifest with Socket Mode disabled, and a human Slack identity is mapped in DSQL.
- Terraform outputs supply the public URLs and DSQL endpoint; the Anthropic webhook signing key has been synced.
- Run `./scripts/verify --fast` during development and `./scripts/verify --full` before deployment. The full gate includes PostgreSQL, semantic Playwright, artifact, and Terraform checks.

## Credentials and deployment

```bash
aws sts get-caller-identity --profile default
export AWS_PROFILE=default AWS_SDK_LOAD_CONFIG=1 AWS_REGION=us-west-2 AWS_DEFAULT_REGION=us-west-2
eval "$(aws configure export-credentials --profile default --format env)"
./scripts/deploy.sh --plan-only
```

Confirm the discovered account is intended and inspect every Terraform delete or replacement before running `./scripts/deploy.sh`. The wrapper applies Terraform, syncs secrets, migrates and seeds DSQL, uploads Web assets, and invalidates CloudFront. Never print exported credentials. Migration 008 intentionally drops the six legacy session-era tables; verify only those tables are removed and current thread/controller state remains.

## Callbacks and identity

- Use Terraform's `slack_events_url`, `slack_interactions_url`, and `anthropic_webhook_url`. Keep Slack Socket Mode disabled and reinstall after scope changes.
- Subscribe the Anthropic webhook to `session.status_idled`, `session.status_terminated`, and `session.budget_reached`. The controller may also wake on `session.status_rescheduled`.
- Map a verified human Slack identity observed in a signed event:

  ```bash
  export DSQL_ENDPOINT="$(terraform -chdir=infra/app output -raw dsql_endpoint)"
  npm run db:map-slack -- --team-id T01234567 --user-id U01234567
  ```

Optional Slack allowlists are traffic filters, not authentication.

## Web and Slack acceptance

1. Sign in to Web with the access token, create an application thread, send a basic turn, and confirm one A2A Task and a final response.
2. Reload during `WORKING`; verify canonical history and the same Task. Complete an approval after reload, then Stop a separate Task during `INPUT_REQUIRED`.
3. From the mapped human Slack account, send an app mention and a bound-thread reply. Confirm one Task per input, exact-message receipt/source links, and completed projection.
4. Allow, deny with reason, and Stop through Slack controls. A signed duplicate delivery must not create a second Task.
5. Open the Slack-bound thread in Web with `?thread=<uuid>`; send a Web turn and confirm one Slack projection from the same A2A context.
6. Verify private A2A rejects an outside-VPC request, relevant DLQs are empty, legacy tables are absent, and no surface payload contains a provider session ID.

Use `scripts/live_a2a_inspect.py` for thread, binding, Task, push, projection, and removed-table metadata. It never needs transcript content. The live Slack user smoke requires the configured human user token, channel, DSQL access, and concrete AWS credentials.

## Failure isolation

| Symptom | First boundary to inspect |
| --- | --- |
| Slack mention does not arrive | Socket Mode, signed HTTP Events Request URL, scope/event subscription |
| Slack input is ignored | Verified team/user identity and existing thread ownership |
| Task does not start | `agent-input`, application claim, private A2A admission, controller scheduler |
| Task is held | Controller Task state and KL-003 operator procedure in [operations](../../docs/operations.md) |
| Web stream closes early | `GetTask` and KL-004 recovery, then re-subscribe |
| Slack output missing | A2A push receipt, EventBridge target, projection claim, Slack API result |
| Web or Slack Stop stalls | Controller cancel request, scheduler reconcile, provider interrupt evidence |

Inspect DLQ messages without deleting them; resolve the first broken boundary before redrive and preserve the same logical IDs.

## Historical acceptance

The 2026-09-02 acceptance established signed Slack HTTP delivery and DSQL identity mapping for the earlier direct-CMA architecture. Later Phase 4 and Phase 5 acceptance established private A2A and AG-UI paths. The 2026-09-28 Phase 6 acceptance is recorded in the [final cutover feature contract](../../docs/feature-contract-final-cutover.md): migration `008`, mapped-human Slack, Web/Slack approval and Stop, cross-surface races, private A2A access denial, IAM, and empty DLQs passed.

## Related knowledge

- [Slack Events API operations](../areas/slack-events-api-operations.md)
- [Slack native agent projection](../areas/slack-native-agent-projection.md)
- [Anthropic Managed Agents operations](../areas/anthropic-managed-agents-operations.md)
- [Aurora DSQL IAM operations](../areas/aurora-dsql-iam-operations.md)
- [Slack native working-state acceptance](slack-native-working-state-acceptance.md)
