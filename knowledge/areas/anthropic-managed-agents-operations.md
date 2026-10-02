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
  - id: anthropic-apply
    resource: "https://platform.claude.com/docs/en/cli-sdks-libraries/cli/apply"
    title: "Manage resources as code with ant apply"
  - id: local-cma-contract
    resource: "../../docs/feature-contract-cma-repo-managed-config.md"
    title: "Repository-managed CMA configuration contract"
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
    title: "Controller-private Managed Agent client"
  - id: local-controller-service
    resource: "../../backend/src/managed_agents_app/cma_controller/service.py"
    title: "A2A controller service"
  - id: final-cutover-contract
    resource: "../../docs/feature-contract-final-cutover.md"
    title: "Final A2A cutover feature contract"
generated:
  by: openai-codex/gpt-5
  at: 2026-09-03T04:34:14Z
---

# Anthropic Managed Agents Operations

## Ownership boundary

Anthropic owns the canonical Managed Agent event log, conversation, tool activity, execution status, and usage. Only the private CMA controller reads or writes provider sessions and events. Web and Slack use authorized application threads and A2A Tasks. Aurora DSQL stores application identity and routing metadata plus controller-private correlation and opaque temporary-object keys; it never stores a transcript.

A provider session is an instance of the configured agent in its environment.
Definitions and genuine identity are tracked under `cma/`. Deployment reconciles
them with ant apply, resolves ID/version/environment from `claude-lock.json`, and
injects them only into controller components. Both session creation paths pin
the exact agent version. Surfaces receive neither these values nor credentials.

## Repository-managed configuration

Use ant >= 1.30.0 and credentials authorized for the lock's origin. Preview with
`npm run cma:plan`, deploy with `./scripts/deploy.sh`, and inspect local identity
with `npm run cma:show`. Normal verification validates files locally without ant
or provider access. Deployment plan-only uses the current lock rather than an
unapplied future version.

Prefer Console Export as code with genuine lock state when adopting resources.
Without it, reproduce configuration and bootstrap replacements; matching names
do not adopt identity. Keep old resources untouched through live cutover. The
initial repository migration uses replacements because no export was available.

Serialize applies/deployments. Retain, review, and commit lock updates even after
partial failure. Console/API edits intentionally block apply; normal scripts never
force or prune. Review the dry-run result because a blocked plan can exit zero.

An agent version applied before an AWS failure is not selected by Lambdas already
pinned to a previous version. Environment configuration is selected by ID and may
affect subsequent sessions before AWS rollout. Rollback requires reverting and
applying the relevant source, redeploying the agent pin, and independently checking
environment behavior. Webhook registration and secrets remain separate.

## Session and Task lifecycle

The controller creates a CMA session while admitting the first A2A Task for a context. It holds subsequent Tasks in FIFO order and uses stable creation and message IDs to reconcile retries. Provider session IDs remain in controller-owned `cma_contexts`; application `agent_threads` and `agent_tasks` hold A2A context and Task IDs only.

An uncertain provider send cannot be retried automatically without authoritative evidence that CMA rejected it. KL-003 holds that Task for operator inspection. Pending user content, denial reasons, and clarification answers use temporary object storage; DSQL stores only opaque keys. The scheduler removes consumed objects and sweeps unreferenced old objects.

## Event submission and reconciliation

The controller submits user input, tool decisions, and interruption to CMA. It reads provider events to build canonical A2A Task history and state. A Task subscription is not replay: clients load `GetTask` history before subscribing and reconcile again after closure or failure. The pinned A2A client may end its iterator after an agent Message (KL-004), so closure alone never means completion.

The browser receives AG-UI events from its bridge; Slack receives A2A Task projection. Neither surface tails CMA events or addresses a provider session.

## Webhooks are controller wakeups

Register the public HTTPS webhook in Claude Console and store the one-time `whsec_` signing secret in Secrets Manager. The deployment subscribes to `session.status_idled`, `session.status_terminated`, and `session.budget_reached`; `session.status_rescheduled` may also wake the controller when sent.

Verify the raw delivery with the Anthropic SDK. For a known controller-private session, schedule its context for reconciliation. Ignore unknown sessions. The webhook does not publish a surface-level session event and does not by itself prove a final agent answer. Controller reconciliation updates the A2A Task; A2A push and the application event sink notify the surfaces.

## Failure handling

- **Session creation response uncertain:** inspect controller state and provider evidence before creating another provider session.
- **Provider input acceptance uncertain:** inspect the held Task and CMA evidence. Retry only with verified no-side-effect; otherwise fail safely or reconcile the accepted input. See [operations](../../docs/operations.md).
- **A2A stream closes early:** use `GetTask` and stable message IDs, then re-subscribe while nonterminal.
- **Webhook returns 503:** check the signing-key secret, controller DSQL lookup, scheduler queue, and Lambda logs.
- **No Slack projection:** inspect the A2A Task, push receipt, application event, and Slack projection lease in that order. Never infer provider output from an idle webhook alone.
- **Budget reached:** inspect controller Task state and provider evidence before permitting more input.

## Deterministic verification

A local semantic test sends a Web or Slack turn through `ThreadAgentService`, the A2A controller, and `FakeCmaProvider`; it checks one Task, canonical history, push delivery, and surface projection without writing transcript content to DSQL. Live provider acceptance checks use real mapped-human Slack and Web turns, signed webhooks, and Task metadata; provider session IDs remain private to operator diagnostics.

## Related knowledge

- [Slack Events API operations](slack-events-api-operations.md)
- [Slack native agent projection](slack-native-agent-projection.md)
- [Aurora DSQL IAM operations](aurora-dsql-iam-operations.md)
- [Managed Agent application end-to-end runbook](../projects/managed-agent-application-e2e-runbook.md)
