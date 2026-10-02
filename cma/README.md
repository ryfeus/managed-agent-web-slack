# Repository-owned Managed Agent

The agent definition, environment definition, and `claude-lock.json` belong in
Git. The lock records provider identity, exact agent version, workspace origin,
and fingerprints; it contains no credentials. Sessions pin the agent version.

Install the official [ant CLI](https://platform.claude.com/docs/en/cli-sdks-libraries/cli/quickstart)
version **1.30.0 or later** on PATH (the wrapper also supports an official local
binary at ignored `.generated/bin/ant`). Authenticate with `ant auth login`, supported
CLI workload identity, or `ANTHROPIC_API_KEY`. The wrapper loads the API key from
the untracked repository `.env` only when it is not already exported. `show` and
`validate` neither load secrets nor require the CLI.

```bash
npm run cma:plan
./scripts/deploy.sh
npm run cma:show
```

Deployment performs `npm run cma:apply` before selecting the updated lock values
for Terraform. For initial bootstrap only, run `npm run cma:apply` explicitly;
normal deployment requires an existing valid lock. Review and commit every lock
update, including identities recorded by a partially failed apply. Scripts never
commit or push. Run **one apply/deployment at a time**: ant does not lock its state.

Apply always names this directory explicitly and never supplies force or prune
flags. Console/API edits intentionally block reconciliation. Inspect the diff
and decide which configuration is intended before an operator-reviewed recovery.
The dry-run command is informational: ant can return success even for a blocked
plan. `deploy.sh --plan-only` uses the current local lock and never contacts CMA.

## Migration and rollback

Console **Export as code** includes genuine lock state capable of preserving
existing resource IDs. Do not invent a lock or assume matching names adopt a
resource. Preserve exported identity when moving source paths, then review a
dry run. Without a valid export, bootstrap replacement resources from the exact
existing configuration and retain the old resources through cutover acceptance.
This repository's initial migration uses that replacement path.

Agent updates create a version selected only when Lambda configuration changes.
If AWS deployment fails, previously pinned Lambdas retain their old selection.
Revert agent source, apply, and redeploy to roll back configuration. Existing
provider sessions continue their previously selected agent configuration.

Environments are selected by ID, **not pinned by version**. Updating an environment
may affect subsequent sessions even before AWS rollout; agent rollback does not
roll it back. Revert environment source, review/apply, and validate new sessions.
Webhook registration and signing secrets remain separate operational work.
