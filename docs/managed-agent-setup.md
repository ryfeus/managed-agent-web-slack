# Managed Agent setup

The application reuses one repository-managed Anthropic agent and environment.
Their definitions and genuine resource identity are tracked under `cma/`; secrets
remain deployment configuration. `claude-lock.json` records origin, resource
paths, IDs, exact agent version, and fingerprints. It is not a credential.

Install [ant CLI >= 1.30.0](https://platform.claude.com/docs/en/cli-sdks-libraries/cli/quickstart)
and configure `ANTHROPIC_API_KEY` in untracked `.env`, or use supported CLI
authentication. Runtime IDs/version are derived from the lock, not local manual
ID values or the retired `AGENT_ID` alias.

```bash
npm run cma:plan
./scripts/deploy.sh
npm run cma:show
```

Real deployment runs ant apply, then pins both production session-creation paths
to `{type: agent, id, version}`. The controller fails clearly before provider use
when its pin, ID, environment, or credential is missing/invalid. Surface-only
components do not require provider configuration. Local E2E supplies synthetic
IDs and version 1 without Anthropic credentials.

## Adoption and bootstrap

For existing resources, prefer Console **Export as code** with its genuine lock.
Normalize definitions while preserving lock identity, then review a dry run for
updates/unchanged resources rather than unexpected creates. Matching source names
alone do not adopt existing resources. Never fabricate a lockfile.

Without a usable export, reproduce the existing configuration, explicitly run
`npm run cma:apply` to bootstrap replacements, and retain old resources until
cutover acceptance. Normal deployment requires valid existing lock state.
The initial migration here follows the replacement path without changing the
current prompt, model, permissions, networking, or package configuration.

Run one apply/deployment at a time. Review and commit lock changes, including
partial failures. Remote edits intentionally block apply; normal scripts never
force overwrite or prune. Inspect the difference and resolve source intent before
an explicit operator-reviewed recovery. A successful CLI dry-run exit does not
prove the displayed plan is unblocked.

## Rollout and rollback

If CMA creates a new agent version and AWS deployment fails, previously pinned
Lambda configuration continues selecting its previous version. Retry deployment
against the retained updated lock. Revert agent source, apply, and redeploy to
roll back configuration; existing sessions retain their original selection.

Environments are selected by ID, not version-pinned. Environment changes may
affect subsequent sessions independently of AWS rollout. Agent rollback does not
roll back an environment; revert/review/apply its source and verify new sessions.
`deploy.sh --plan-only` reads the current local lock and never applies CMA. Preview
pending provider changes separately with `npm run cma:plan`.

## Local A2A controller

With a local Postgres database migrated and controller configuration loaded, run
`./scripts/cma-controller-local.sh` from the repository root. It binds to
`127.0.0.1:8081` by default. When
`CMA_SCHEDULER_QUEUE_URL` is unset, the ASGI lifespan starts a local scheduler
that executes one bounded reconciliation per wakeup. Its disk-backed pending
input directory defaults to `/tmp/cma-pending-input`; set
`CMA_PENDING_INPUT_DIR` to retain it elsewhere. For a deployed controller, use
the dedicated SQS scheduler queue instead.

The local launcher loads `.env` secrets and derives its provider configuration
from the lock. For a direct administration command, first load your secrets and
source `scripts/cma_env.sh` with `CMA_REPOSITORY_ROOT` set to the repository root.
The separate `backend/run_cma_controller.sh` is the packaged Lambda launcher;
runtime packages never read repository files or the lockfile.

For a held ambiguous operation, use
`uv run --directory backend python -m managed_agents_app.cma_controller.admin list-held`
or `inspect TASK_ID`. `fail TASK_ID` terminalizes the held task. Retry is limited
to ambiguous input delivery and requires external confirmation that CMA did not
accept it:
`retry TASK_ID --verified-no-side-effect`. Resolution commands require the
configured scheduler queue and are controller-admin operations, not A2A APIs.

## Webhook

Webhook provisioning and signing-secret lifecycle remain separate from CMA apply.

After deployment:

1. Read `anthropic_webhook_url` from `terraform -chdir=infra/app output`.
2. In Claude Console, create a webhook for `session.status_idled`,
   `session.status_terminated`, `session.status_rescheduled`, and `session.budget_reached`.
   The rescheduled event also wakes the controller scheduler.
3. Store the one-time signing key in `.env` as `ANTHROPIC_WEBHOOK_SIGNING_KEY`.
4. Run `npm run secrets:sync` with concrete AWS credentials.

The controller webhook verifies the raw payload with the official SDK and wakes
the scheduler only for a known controller-private session. Unknown sessions are
ignored. The scheduler reconciles A2A Task state; the Slack projector consumes
that state and never treats an idle webhook itself as an agent answer.

## Transcript invariant

Session events are replayed directly from Anthropic. The same pure reducer handles
replayed and live events, and the browser does not send old message history with a
new turn.
