# Upgrading an existing installation

This release changes policy, database metadata, Slack behavior and the Web serving
stack. Fresh installations can use the final release directly. Existing Python
Web installations must use the staged public commits described below. Keep the
existing `APP_NAME`, deployment environment, state bucket, CMA identities and
cookie secret; changing those is not part of this upgrade.

## Preparation and policy migration

1. Review [limitations](known-limitations.md), [DSQL operations](dsql.md), and
   [Managed Agent setup](managed-agent-setup.md). Save the current Terraform state
   and deployment artifact/version references in private operator storage. Do not
   commit state, credentials or live test artifacts.
2. Stop accepting new work, let active Tasks settle, and pause scheduler/push and
   input consumers before changing schema or controller code. Inspect pending
   approvals and held/uncertain confirmations using the controller's operator
   commands. Do not reset attempted confirmations or resend ambiguous input.
3. Install Node 24, Python 3.14 with uv, Rust 1.99.0, Docker Compose, Terraform and
   ant >= 1.30.0. Run `npm ci`, `uv sync --directory backend --locked`, and
   `npx playwright install chromium` for each checkpoint whose locks change.
4. Use the operator's genuine `cma/claude-lock.json`. The example repository lock
   belongs to its originating workspace; never fabricate resource IDs or replace
   an installation's lock with it. Review `npm run cma:plan` for the added
   `ask_user` custom tool, then explicitly apply the definitions and retain all
   lock updates, including partial failures. Agent versions are pinned; environment
   changes are not version-pinned. No automatic force/prune is permitted.
5. Verify STS identity, export concrete AWS credentials and select the existing
   Terraform backend. Use the endpoint from that deployment's Terraform outputs.
   Apply migrations `009` through `012` in order with `npm run db:migrate` under
   quiescence. `010` drops obsolete push-receipt/observation tables and a Task
   observation column; `011` removes policy snapshot metadata after normalizing
   only eligible unattempted requests; `012` adds native review metadata. These
   are not transcript migrations. Interrupted DSQL DDL must be resumed through
   the migration runner. Old controller code is not a safe rollback after these
   removals; forward recovery or an explicitly planned restoration is required.

## Web rollout checkpoints

This commit is the **CloudFront cutover checkpoint**. Its parent is the additive
checkpoint, which retains the Python Web origin. This checkpoint routes `/api/*`
to the IAM-protected TypeScript gateway and retains both Python Web Lambdas and
their routes for rollback. Verify standalone connectivity before applying it,
then verify authentication, history, review, clarification, Stop and Web/Slack
continuity before proceeding to final retirement. Keep previews disabled.

At each checkpoint, keep `PREVIEW_ENABLED=false`. Use
`npm run deploy:plan -- --application-only` to save a target-account plan, review
it, and apply only with deployment authorization. `--application-only` skips CMA
apply, secret synchronization and database migration/seed; complete required
schema, secrets and role setup explicitly. It does not bypass deletion guards.
Preserve DSQL deletion protection and all data resources. Resume paused consumers
only after schema and deployed controller code agree, and verify canonical Tasks
before allowing new work.

For the new Web role, grant the existing `app_runtime` database role using the
role ARNs from Terraform outputs. Verify IAM denial on the unsigned standalone
endpoint, authorized access through CloudFront, private controller access and DSQL
connectivity. Existing authentication secrets remain in Secrets Manager.

## User-visible changes

- The supported Slack baseline includes mentions/DMs, owned bound replies,
  processing/reactions, task cards with final-reply fallback, source links, Stop
  and native policy review. Legacy feature flags are removed. Message shortcuts,
  feedback controls, active-context handling and unfurls are removed.
- Render and review the new Slack manifest, preserving existing organization and
  user OAuth settings when updating an app. Keep Socket Mode disabled and reinstall
  after scope changes. A manifest update alone does not prove installed scopes or
  revoke previously granted scopes.
- OpenAPPA evaluates each approval in fresh memory. Automatic policy may decide
  without a human; a supported native HITL offer exposes exact-call review. First
  authorized ruling wins, and only native resolution can authorize confirmation.
  Explicit Stop remains independent. Clarification is answered through Web.
- Local development is `npm run dev`; setup is explicit. Replace old verification
  wrappers with `npm run check`, `npm run verify:full` and
  `npm run secrets:check`. Local runs never read `.env` or contact live providers.

## Acceptance and rollback

Run the full local suite on the final public tree. Separately authorized live
acceptance must check cookie authentication, history/reload, canonical completion,
Allow/Deny, clarification, Stop and Web/Slack continuity. Use API-only Slack tests.
Signed fixtures and recorded APIs do not establish native Slack-client rendering.

Before Python retirement, the cutover can be reversed by reviewing a plan that
restores the old CloudFront origin while retaining its Lambdas/routes. After
retirement, prefer an already tested retained Web Function revision through
`web_function_rollback_revision`; changing only the origin cannot restore deleted
Python resources. This Web rollback does not reverse database or CMA changes.
