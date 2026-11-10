# Upgrading an existing installation

This release changes policy, database metadata, Slack behavior and the Web serving
stack. Fresh installations can use the final release directly. Existing Python
Web installations must use the staged public commits described below. Keep the
existing `APP_NAME`, deployment environment, state bucket, CMA identities and
cookie secret; changing those is not part of this upgrade.

Installations already using the TypeScript Web gateway and migration 012 can go
directly to [custom tools and persistent policy](#custom-tools-and-persistent-policy).
Older Python Web installations must still complete the preparation and Web
checkpoints below before this application update.

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

The following commits exist in this public repository. Use a separate clean
checkout for each stage and preserve your deployment-specific configuration.

| Stage | Public commit | Behavior |
| --- | --- | --- |
| Additive | `7907ef92e85918f7861fdd48f05d35031b93a877` | Adds policy/controller changes, standalone Web gateway and AppSync; Python Web remains the CloudFront origin. |
| Cutover | `9a4488497077d9505b3f162e6ac039ca0bb3ac69` | Routes `/api/*` to TypeScript; retains Python Web Lambdas/routes for rollback. |
| Retirement | Final release containing this runbook | Removes Python Web resources and adds optional Slack title previews. |

Check out the additive commit first, complete the schema/CMA preparation and
review the target-account plan. Verify the standalone gateway's IAM, private A2A
and DSQL connectivity. Then check out the cutover commit and review its plan.
Verify cookie login, canonical history/reload, human review, clarification, Stop
and Web/Slack continuity through CloudFront before proceeding to retirement.

The final plan intentionally removes superseded Web resources and is rejected by
the normal deletion guard. Inspect the saved plan and exact resource addresses;
only an explicitly authorized operator may apply that reviewed retirement plan
with Terraform. Do not weaken the guard. If a combined plan has a dependency cycle,
review separate route/permission and Lambda/role/log/artifact retirement steps.
Confirm the deployed API stage no longer exports the old Web paths. Preserve DSQL,
buckets, queues, networks, APIs, stages and the CloudFront distribution.

Keep `PREVIEW_ENABLED=false` and, on the final release,
`SLACK_PREVIEW_ENABLED=false`. Preview enablement is separate opt-in acceptance;
see [Web rollout](phase-5-rollout.md) and [Slack previews](phase-6-rollout.md).

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

## Custom tools and persistent policy

This update adds application-owned custom tools, native SQLite/S3 policy state,
and exact admitted-output delivery. Upgrade the application and native helper
together to protocol **6**; intermediate protocol 5 artifacts are not a required
public upgrade checkpoint.

1. Stop new admission, drain or explicitly cancel active Tasks, and pause input,
   scheduler and reconciliation consumers/triggers. Settle pending reviews and
   uncertain execution/delivery under the current code before replacing workers.
   Preserve attempt markers and retain private recovery artifacts.
2. Preserve the installation's genuine CMA lock, resource identities, Terraform
   backend and secrets. The repository lock records the originating workspace's
   agent version 4; it is not a replacement for another installation's lock.
   Review `npm run cma:plan` for `text_metrics` and the four synthetic `demo_*`
   tools. Reconcile through the CLI; never fabricate IDs or edit lock fields.
3. After verifying account identity and exporting concrete credentials, apply
   migrations **013, 014 and 015** in order with `npm run db:migrate`. They add
   custom execution metadata, context checkpoint metadata and admitted-result
   references. DSQL retains metadata only; raw/admitted bodies and native SQLite
   snapshots stay in private object storage. No transcript migration occurs.
4. Build matching Python/native protocol 6 artifacts and review
   `npm run deploy:plan`. The scheduler gains access to `policy/*` in the existing
   private bucket and a custom-tool hold alarm; no new storage service is needed.
   The normal explicit deployment updates CMA before AWS, so keep admission and
   consumers paused throughout. `--application-only` skips CMA reconciliation,
   secrets and migrations; operators using it must complete those prerequisites
   separately. Preserve the normal saved-plan and destructive-change review.
5. Verify schema, registry, packaged policy/helper, scheduler IAM permissions and
   the installation's lock-selected CMA version agree before resuming consumers
   and admission. Use **new conversations** for acceptance and the demos. Existing
   CMA history is not imported into native policy state; no retroactive coverage
   is provided. Existing sessions retain their agent definition, and persisted
   contexts pin exact policy bytes.

For separately authorized acceptance, check custom-tool completion through Web
and Slack, exact redacted output, persisted restrictions, review Allow/Deny,
reload and Stop. Use the API-only suites in [testing](testing.md); signed Slack
fixtures do not establish native-client rendering. Local verification never
deploys or runs live acceptance.

Rollback requires drained work and new conversations with matching agent,
backend and native artifacts. Never resume stateful conversations with stateless
workers or clear checkpoints to bypass a policy-version hold. Retain the additive
tables/columns, current checkpoints and recovery objects; no down migration is
provided. The optional orphan sweep preserves current checkpoints and removes
only unreferenced objects older than 24 hours. See [operations](operations.md)
for missing-state, withheld-output and uncertain-delivery recovery.

## User-visible changes

- The supported Slack baseline includes mentions/DMs, owned bound replies,
  processing/reactions, task cards with final-reply fallback, source links, Stop
  and native policy review. Legacy feature flags are removed. Message shortcuts,
  feedback controls, active-context handling and unfurls are removed.
- Render and review the new Slack manifest, preserving existing organization and
  user OAuth settings when updating an app. Keep Socket Mode disabled and reinstall
  after scope changes. A manifest update alone does not prove installed scopes or
  revoke previously granted scopes.
- OpenAPPA uses fresh helpers reopening persistent native SQLite state. Automatic
  policy may decide without a human; a supported native HITL offer exposes
  exact-call review. First authorized ruling wins, and only native resolution
  can authorize confirmation. Registered custom tools also undergo native result
  admission. The synthetic demos add persisted restrictions, effect-history
  review and output redaction. Explicit Stop remains independent. Clarification
  is answered through Web.
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
