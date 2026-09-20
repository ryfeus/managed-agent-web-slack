# Repository Agent Context

## Purpose and invariant

This repository implements a serverless web and Slack interface for Anthropic Managed Agents. A user can continue the same Claude session from the assistant-ui web application or from a Slack thread.

Preserve this architectural invariant:

> Anthropic owns conversation transcripts and execution state. Aurora DSQL stores only identity, authorization, session ownership, surface routing, idempotency, and projection state.

Do not add Claude messages, tool calls, reasoning, or transcript copies to DSQL.

## Repository map

- `apps/web`: statically exported Next.js/assistant-ui frontend.
- `apps/web/lib/managed-agent-reducer.ts`: browser-only Managed Agent event reducer.
- `backend/src/managed_agents_app`: Python 3.14 backend shared code and all six Lambda handlers.
- `backend/migrations`: language-neutral DSQL schema migrations.
- `backend/tests`: pytest unit and contract coverage.
- `slack/manifest.template.yaml`: deployment-independent Agent View, event, interactivity, shortcut, and unfurl template.
- `infra/bootstrap`: shared Terraform state-bucket bootstrap.
- `infra/app`: application infrastructure and remote state configuration.
- `scripts/deploy.sh`: validated build, Terraform apply, secret sync, DSQL migration/seed, and frontend upload.
- `scripts/verify`: authoritative fast and full local verification entrypoint.
- `docs/feature-contract-template.md`: behavioral contract template for substantial distributed changes.
- `knowledge/known-limitations.json`: machine-checked correctness limitations and their executable tests.
- `knowledge/persistence-schema-contract.json`: machine-checked inventory of migration-owned tables and columns.
- `knowledge`: reusable operational knowledge and project acceptance runbooks; keep its immediate indexes and `knowledge/log.md` current when adding concepts.

## AWS and Terraform

- Region default: `us-west-2`; do not claim arbitrary-region support without validation.
- Preferred local SSO profile: `default`, configured through `AWS_PROFILE`.
- Discover the target account with STS. `EXPECTED_AWS_ACCOUNT_ID`, when set, is a safety assertion rather than a default.
- Remote state is deployment-specific and rendered under `.generated/terraform/`; use `TF_STATE_BUCKET` to target an existing bucket.
- Application Terraform root: `infra/app`.
- Use the `aws-current-account`, `aws-terraform-credentials`, and `terraform-remote-state` skills when applicable.
- Always verify `aws sts get-caller-identity --profile "$AWS_PROFILE"` before applying infrastructure.
- With SSO, export concrete credentials before direct Terraform or Python AWS SDK operations. `scripts/deploy.sh` already does this correctly.
- Use `./scripts/deploy.sh` for normal deployments; it validates required values, renders backend configuration, and syncs secrets after Terraform provisions their containers.

Treat Terraform outputs as authoritative for application URLs, callbacks, DSQL endpoints, and resource IDs. Never commit generated deployment outputs.

## Secrets and configuration

- Local secrets are in `.env`; never print, commit, or copy their values into documentation or Terraform state.
- `.env.example` documents supported variables.
- Required deployment inputs include `CLAUDE_AGENT_ID`, `CLAUDE_ENVIRONMENT_ID`, the Anthropic API key, and web access/cookie secrets. `AGENT_ID` is only a compatibility alias.
- The Anthropic webhook signing key is created when the webhook is registered and then synced to Secrets Manager.
- `npm run secrets:sync` invokes the Python operation and derives each Secrets Manager region from its Terraform ARN.

## Slack trust and routing

- Production delivery uses Slack's HTTP Events API. **Socket Mode must remain disabled** or Slack will route events to WebSocket instead of the AWS Request URL.
- Render and import `.generated/slack-manifest.yaml` with `npm run slack:manifest`, keep Socket Mode disabled, and reinstall after scope or event changes.
- The manifest subscribes to mentions, DMs/channel replies, Agent View lifecycle/context, stop, and shared links. Top-level channel messages are accepted only as `app_mention`; a plain channel message is accepted only when it is a reply to an existing DSQL binding.
- Bot scopes are `assistant:write`, `chat:write`, `app_mentions:read`, `im:history`, `channels:history`, `groups:history`, `commands`, `links:read`, `links:write`, and `reactions:write`.
- Reinstall the Slack app after importing any manifest change that adds a scope. A successful `reactions.add` call is the acceptance check for `reactions:write`; manifest text alone does not prove the installed token has the scope.
- Authenticate ingress only with Slack's timestamped HMAC signature.
- Derive `team_id` and the human `event.user` from the verified event, then resolve `(provider, tenant_id, external_user_id)` through DSQL `external_identities`.
- `SLACK_TEAM_ID` and `SLACK_USER_ID` are optional development allowlist filters. They are not authentication credentials and should normally remain unset.
- Add another verified identity with:

  ```bash
  export DSQL_ENDPOINT="$(terraform -chdir=infra/app output -raw dsql_endpoint)"
  npm run db:map-slack -- --team-id T01234567 --user-id U01234567
  ```

  Run this with valid concrete AWS credentials and `AWS_REGION=us-west-2`.

## Slack native working state

- Emit `SlackWorkStarted` only after the ingress claim, identity authorization, command/text validation, and existing-binding ownership check succeed. Emit it before delivering input to Claude so Slack can show immediate `processing` state and an optional receipt reaction.
- Treat working-state projection as asynchronous UX. Slack status, reaction, or permalink failures must not prevent the authorized Claude input from running; EventBridge retry and the target DLQ isolate projector failures.
- Preserve the exact triggering `message_ts` separately from the thread root. Reactions and source permalinks belong to that exact message, including an unmentioned nested reply.
- Slack streaming messages have one mode for their lifetime. Task-card streams start with `chunks`, append response text as `markdown_text` chunks, and stop with chunks. Do not mix the text-mode `markdown_text` request field with chunk mode.
- Start a task-card stream immediately with a timeline `task_update` and an optional “Original message” URL. Never project thinking or chain-of-thought.
- Mark the task complete only when no tool approval is pending. A pending tool leaves the task in progress, stops the stream with `suspended`, and renders separate Allow/Deny controls.
- `SLACK_TASK_CARDS_ENABLED=true` requires `SLACK_STREAMING_ENABLED=true`. When streaming is disabled, log the invalid combination and fall back with task cards disabled.
- Current sandbox gates are receipt reaction, source links, task cards, streaming, Agent View, tool approvals, feedback, shortcuts, active context, bound-thread replies, and unfurls. Defaults remain disabled in configuration examples so deployments opt in deliberately.

## Managed Agent stream reconciliation

- A Managed Agent event subscription is not a replay mechanism. Fast turns can finish before the Slack projector subscribes, leaving a live subscription waiting even though the canonical session is already idle.
- After creating the early Slack task card, list canonical session events before subscribing. If the current turn already has an agent message plus an idle/terminal status, render that final message without opening the live subscription.
- After a stream error or timeout, list canonical events again. Use the retained final agent message or pending-tool state when available; retry only when the turn is still genuinely incomplete.
- Break the live subscription as soon as the final `agent.message` arrives. A final message must be appended even when no text deltas were observed.
- The completion webhook must defer while a valid live-stream lease exists. Recovery may finalize only a stream explicitly marked for fallback or one whose lease expired; otherwise the webhook can call `chat.stopStream` before live delta appends and cause `message_not_in_streaming_state`.
- Record the final Managed Agent event-to-Slack timestamp receipt when the stream closes. Keep stream leases, cursors, IDs, attempts, and errors in DSQL, but never message bodies.

## Anthropic Managed Agent behavior

- Load the agent ID and API credential from `.env`; do not hardcode them.
- The deployed webhook subscribes to `session.status_idled`, `session.status_terminated`, and `session.budget_reached`.
- Web and Slack surfaces are authorized against the same DSQL principal, allowing an explicitly linked Slack thread to continue a web-created session.
- The application relies on Anthropic session/event APIs for transcript reads and streams.

## Validation

Use the fast verifier repeatedly while implementing:

```bash
./scripts/verify --fast
```

Before declaring a change complete, run the authoritative local verifier:

```bash
./scripts/verify --full
```

Individual npm, pytest, E2E, artifact, and Terraform commands remain available for
focused debugging. `npm run e2e:live` is separate, credentialed, and never part of
normal verification.

The fast verifier enforces closed-by-default rails before linting:

- Provider SDK imports require an exact path exception. AWS SDK imports belong
  only in configuration, EventBridge, and administrative operations; Slack SDK
  imports belong only in Slack adapters and their direct retry test; Anthropic SDK
  imports belong only in the Managed Agent adapter and the narrow webhook
  signature verifier. Ports and neutral model/domain/wire modules must not import
  provider implementations.
- Every migration-owned table and column must appear in
  `knowledge/persistence-schema-contract.json`. Update migrations and the contract
  together, while preserving the independent prohibition on transcript, message,
  tool-output, and reasoning storage.
- New test skips, fixmes, and expected failures fail verification unless the same
  file is the executable test for an active `open` or `accepted` known limitation.
  Resolved limitations cannot authorize disabled tests. Only the exact PostgreSQL
  and live-service environment gates are permanent harness exceptions.

## Autonomous change protocol

Before changing code, read the relevant invariants and tests. For a substantial
change to distributed behavior, protocols, persistence, authorization, or an
external integration, create or update a feature contract from
`docs/feature-contract-template.md`. Preserve Runtime and port boundaries;
handlers must not construct infrastructure or provider clients.

During implementation, run `./scripts/verify --fast`, add tests at the innermost
layer capable of proving the behavior, and include deterministic duplicate,
failure, race, or concurrency coverage when those semantics change. Record any
material unresolved correctness gap in `knowledge/known-limitations.json` and
reference its stable ID from the executable test that demonstrates it.

Do not add an SDK allowlist path, persistence-contract entry, or permanent test
exception as a convenience. Each is an architectural decision and must include a
checker self-test. When a limitation is resolved, enable its test and resolve or
remove the registry entry in the same change.

Before completion, run `./scripts/verify --full`, review the result against the
feature contract, and ensure no known limitation was silently removed. Do not claim
real-provider compatibility unless an existing live contract test proves it;
otherwise state the remaining live verification requirement explicitly.

When debugging, inspect the `slack-ingress`, `agent-input`, `anthropic-webhook`, and `slack-projector` Lambda log groups in that order. Ingress diagnostics intentionally log event metadata and field presence, never Slack message text.

See `knowledge/areas/slack-native-agent-projection.md` and `knowledge/projects/slack-native-working-state-acceptance.md` for the reusable projection rules and acceptance procedure.
