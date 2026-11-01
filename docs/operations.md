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

## Slack setup and Phase 4 rollout

First deploy with Claude/Web credentials, then import the generated manifest, configure Slack credentials/identity mappings, and redeploy. Set `PUBLIC_APP_URL` from Terraform `application_url` when needed. There is one Slack baseline without capability flags; source permalinks require a configured public URL. Reimport/reinstall the manifest after changed scopes/events. Socket Mode stays disabled. See [Slack setup](slack-setup.md).

Migration `012_openappa_human_review.sql` adds only four nullable metadata fields to existing requests. It does not delete rows or resources and reuses the existing temporary-input bucket. Apply migrations before running new scheduler/controller code; for an existing sandbox, quiesce scheduler consumers through migration/code rollout and resume after the versions agree. Do not run old automatic-only schedulers against pending human reviews: rolling back code requires first settling/canceling those reviews under the new code. Migration 011's older snapshot transition has its own [quiescence/retention notes](../knowledge/areas/openappa-cma-policy-state.md).

DSQL rejects constraints on `ALTER TABLE ADD COLUMN`; the migration therefore adds `human_ruling` as nullable text. The fenced repository transition validates its allow/deny domain. Migration statements are idempotent so a partially applied additive migration can be completed without resetting data.

This Phase 4 change removes Slack configuration entries and installed interaction features, not Terraform resources. A saved plan is still required against the actual target; source inspection is not a deployment plan. No live data cleanup is authorized by interface simplification.

## Recovery

Inspect Slack ingress → agent-input → anthropic-webhook → slack-projector, with cma-controller/scheduler/push and a2a-event-sink to locate intermediate boundaries. Web uses web-api → agui-bridge → controller/scheduler. Correlate event, thread and Task IDs, not user bodies/tool arguments. Inspect paired input/scheduler/push DLQs and the EventBridge DLQ before redrive; preserve logical IDs and diagnose the first broken boundary.

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
