# Operations

## Plan and deploy

Use `AWS_PROFILE=default` unless configured otherwise, default region `us-west-2`, and the existing deployment's `TF_STATE_BUCKET`. Verify `aws sts get-caller-identity --profile "$AWS_PROFILE"` before applying. `EXPECTED_AWS_ACCOUNT_ID` is an optional exact-account assertion. With SSO, export concrete credentials before direct Terraform/Python AWS use; the deployment wrapper handles this itself. Never print credentials.

```bash
npm run cma:plan
npm run deploy:plan
npm run deploy
```

Plan-only validates local CMA definitions/lock and credentials, discovers the account, renders ignored backend configuration, requires the existing state bucket, builds artifacts, and saves `dist/application.tfplan`. It never applies CMA/Terraform, bootstraps a bucket, syncs secrets, migrates DSQL or uploads Web files. It refuses deletion/replacement of protected DSQL, S3, CloudFront, API Gateway, Lambda, SQS, EventBridge, Secrets Manager and IAM resources. Investigate every refusal; do not weaken the guard to get a deployment through.

Normal deployment applies CMA definitions, resolves the exact agent version/environment from `cma/claude-lock.json`, builds artifacts, applies the reviewed Terraform plan, syncs supplied secrets to Terraform-created Secrets Manager containers, migrates/seeds DSQL, uploads static Web and invalidates CloudFront. It renders `.generated/slack-manifest.yaml` from authoritative Terraform outputs. Do not commit generated backend/output/plan files. Shared versioned Lambda S3 artifacts are described in the [deployment contract](contracts/lambda-s3-deployment.md).

Serialize applies. Retain, review and commit partial CMA lock updates; never automatically force/prune or discard them. Remote edits block normal apply, and a successful dry-run exit can still display a blocked plan. Revert/apply/redeploy the intended agent definition for rollback. Environments are ID-based and require independent rollback/new-session validation. See [Managed Agent setup](managed-agent-setup.md).

## Phase 5 cutover

Follow [the public upgrade checkpoints](upgrading.md) and [Web rollout notes](phase-5-rollout.md). `npm run deploy:plan -- --application-only` saves a plan without CMA changes. Application-only deployment also skips secret synchronization and database migration/seed; complete required schema and role setup explicitly. The final tree removes Python Web resources, so existing installations must first complete additive and cutover acceptance. Deletion guards remain unchanged. Retained Web Function revisions provide rollback after retirement; use Terraform outputs and endpoint revision weights for the actual deployment.

## Phase 6 Slack previews

[Slack preview rollout](phase-6-rollout.md) requires the producer plus a separate consumer role and API-only live acceptance. Both switches default to false. Verify the new role's DSQL association and actual IAM subscription before enabling it. No new Slack scopes or database migration are needed. Disable `SLACK_PREVIEW_ENABLED` and its mapping to roll back optional titles while preserving canonical P0 output.

## Slack setup and Phase 4 rollout

First deploy with Claude/Web credentials, then import the generated manifest, configure Slack credentials/identity mappings, and redeploy. Set `PUBLIC_APP_URL` from Terraform `application_url` when needed. There is one Slack baseline without capability flags; source permalinks require a configured public URL. Reimport/reinstall the manifest after changed scopes/events. Socket Mode stays disabled. See [Slack setup](slack-setup.md).

Migration `012_openappa_human_review.sql` adds only four nullable metadata fields to existing requests. It does not delete rows or resources and reuses the existing temporary-input bucket. Apply migrations before running new scheduler/controller code; for an existing sandbox, quiesce scheduler consumers through migration/code rollout and resume after the versions agree. Do not run old automatic-only schedulers against pending human reviews: rolling back code requires first settling/canceling those reviews under the new code. Migration 011's older snapshot transition has its own [quiescence/retention notes](../knowledge/areas/openappa-cma-policy-state.md).

DSQL rejects constraints on `ALTER TABLE ADD COLUMN`; the migration therefore adds `human_ruling` as nullable text. The fenced repository transition validates its allow/deny domain. Migration statements are idempotent so a partially applied additive migration can be completed without resetting data.

This Phase 4 change removes Slack configuration entries and installed interaction features, not Terraform resources. A saved plan is still required against the actual target; source inspection is not a deployment plan. No live data cleanup is authorized by interface simplification.

## SQLite/S3 persistence rollout

Migration `014_policy_checkpoints.sql` adds three context metadata fields. Its original rollout used Python/native protocol **5** artifacts and the scheduler-only `policy/*` S3 permission; current upgrades also require migration 015 and protocol 6 below. No new bucket, service or native PostgreSQL schema is required. Historical acceptance in the originating sandbox on 2026-11-08 verified these changes; see [persistence evidence](testing.md#sqlites3-persistence). Local verification never deploys them automatically.

Stop new admission, drain/cancel active Tasks, and quiesce scheduler consumers and reconciliation before migration and the coordinated backend update. Run the normal plan review and explicit deployment steps. Resume with **new application conversations** for a clean policy boundary: existing CMA history is not imported into native policy state. If an old context is deliberately continued, its first new evaluation begins the persistence epoch; pre-cutover calls/results are not covered. Never claim retroactive information-flow coverage. The original persistence rollout used an automatic baseline; the current default policy also includes the stateful synthetic demos.

A trajectory pins exact policy bytes. A later policy change produces `policy-version-changed`; finish/cancel the old work and start a new conversation rather than clearing the checkpoint. Rollback to stateless code likewise requires settling active work and new conversations; never reuse a stateful conversation under a worker that discards its labels. Retain checkpoint columns/objects for recovery; there is no automatic down migration.

`policy-checkpoint-unavailable`, `policy-checkpoint-invalid`, `policy-checkpoint-too-large` and `policy-version-changed` are metadata-only call hold reasons; custom-result recovery prefixes these with `tool-`. Retry transient reads/uploads; missing/corrupt committed objects require restoring the exact object from an operator-owned backup or failing the Task. Never clear a pointer, execution marker or withheld admission to force progress. Unacknowledged uploads are harmless unreferenced objects; unacknowledged DSQL commits are recovered from the atomic decision/pointer publication.

The existing optional daily orphan sweep also handles `policy/`: it preserves every current context checkpoint and deletes only unreferenced snapshots older than 24 hours. Keep it enabled for sustained use or invoke the existing maintenance path deliberately. Current snapshots retain private native inputs/results for the context lifetime, including completed Tasks. There is no automatic context expiry or compaction; context deletion/retention remains an operator decision. Do not apply an age-only S3 expiration rule to current checkpoints. Each snapshot is capped at 64 MiB; hitting the limit holds instead of dropping history. Whole-file restore/upload and native replay grow with history. No Lambda latency benchmark is claimed.

## Recovery

Custom tools require additive migration `013_cma_tool_executions.sql` and matching Python/native artifacts (currently protocol 6). For rollout, stop new admission, drain or explicitly cancel active Tasks, and pause scheduler consumption plus reconciliation triggers. Apply migration 013 before enabling the new worker. Review `cma:plan` and `deploy:plan`, then explicitly deploy; the normal wrapper updates CMA before AWS, so keep admission/consumption quiescent throughout. Resume only after migration, registry, policy, worker and the lock-selected agent version agree. Never edit the lock by hand. Existing sessions retain their pinned agent definition.

Rollback requires settling/canceling custom invocations under the new code first, then reverting the agent declaration and matching backend/native artifacts under the same quiescence. Retain the additive table and temporary objects; do not drop execution markers or replay an old session against a worker without its registered handlers. This change does not itself authorize deployment or live acceptance.

`cma_custom_tool_held` logs contain only Task identity and a reason code. `tool-execution-uncertain` requires inspecting the external destination and the private invocation record; it cannot use the send-only admin retry command. `tool-delivery-uncertain` requires canonical CMA result evidence. `tool-result-withheld` has no release override. Policy outages retry only result admission using the saved policy/authorization. Stop or `fail TASK_ID` settles an unrecoverable hold without rerunning the handler. After terminal settlement, the existing orphan sweep can remove its temporary objects; the daily sweep remains operator-controlled and disabled by default. Never clear attempt markers to force a retry.

Inspect Slack ingress → agent-input → anthropic-webhook → slack-projector, with cma-controller/scheduler/push and a2a-event-sink to locate intermediate boundaries. Web uses CloudFront → Node Web Function → private A2A controller/scheduler. Optional preview failures are separate metadata counters; canonical P0 remains authoritative. Correlate event, thread and Task IDs, not user bodies/tool arguments. Inspect paired input/scheduler/push DLQs and the EventBridge DLQ before redrive; preserve logical IDs and diagnose the first broken boundary.

Private A2A uses the VPC endpoint; Terraform `private_a2a_url` is authoritative and must reject external callers. A2A subscription closure is not completion: read canonical `GetTask` history and reconcile stable message IDs. Slack finalization uses short current-Task projection, independent of subscriptions/preview.

Working-state errors never cancel/resubmit CMA. If a task-card stream cannot start, canonical output falls back to a normal reply. A `missing_scope` diagnostic requires checking installed scopes and reinstalling, not another feature toggle. Receipts/claims recover ordinary duplicate deliveries; an accepted external write with a lost acknowledgment remains an ambiguity.

For a held provider operation, use the controller administrative commands with concrete credentials:

```bash
uv run --directory backend python -m managed_agents_app.cma_controller.admin list-held
uv run --directory backend python -m managed_agents_app.cma_controller.admin inspect TASK_ID
```

Inspect canonical provider evidence for the exact send. Only after proving CMA did not accept it use `retry TASK_ID --verified-no-side-effect`; otherwise reconcile or `fail TASK_ID`. Never infer non-acceptance from missing Slack output. Uncertain tool confirmations retain their attempt markers and cannot be blindly retried. The metadata inspector `scripts/live_a2a_inspect.py` can inspect bindings/Tasks without transcript output. [Known limitations](known-limitations.md) explains the remaining timing and UI gaps.

Keep DSQL deletion protection and Terraform locks intact; do not force-unlock active operations. Map verified Slack identity with the Terraform DSQL endpoint and concrete AWS credentials:

```bash
export DSQL_ENDPOINT="$(terraform -chdir=infra/app output -raw dsql_endpoint)"
npm run db:map-slack -- --team-id T01234567 --user-id U01234567
```

## Policy demo and admitted-output rollout

Migration `015_admitted_tool_results.sql` adds nullable admitted-object key/digest metadata only. Build protocol **6** Python/native artifacts together. No new service, bucket or permission is needed: admitted output uses the existing private pending-object prefix, live-reference retention and terminal/orphan cleanup. Metadata-only holds include `tool-admitted-result-missing`, `tool-admitted-result-mismatch` and existing recovery/withheld codes; never log or manually release either raw or admitted bodies.

Stop admission, drain or explicitly cancel active Tasks, and quiesce scheduler consumers/reconciliation. Apply migration 015 and coordinate the backend/default-policy change with the reviewed `cma:plan`/`deploy:plan` and explicitly invoked deployment. The normal deploy updates CMA before AWS, so remain quiescent until both agree. Resume only after matching migration, protocol, registry and lock-selected CMA version are verified. Existing sessions retain their agent definition and exact policy digest: start new conversations for the demos, never clear old checkpoint pointers. Rollback likewise needs drained work and new conversations; leave additive metadata and private checkpoints intact.

`scripts/prepare_openappa.py` materializes revision `c7c1e1ef36aa07604b421c48be266d7397557656`, verifies a clean checkout and applies `services/openappa/patches/offer-kind.patch`. Local and Linux builds consume the same source. This one-line visibility patch exposes native read-only offer classification; it changes no native algorithm. Revisit the patch and semantic tests when upgrading OpenAPPA. See [demo prompts](policy-demos.md).

Historical sandbox acceptance on 2026-11-08 verified protocol 6, migration 015 and the updated agent with new conversations. Those results do not establish deployment or acceptance for another installation; see [acceptance evidence](testing.md#stateful-policy-demos-and-admitted-output).
