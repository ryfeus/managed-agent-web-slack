# Operations

## Deployment order

`scripts/deploy.sh` performs a repeatable real-provider deployment:

1. Load untracked `.env`, validate API/web credentials, and locally validate the
   CMA definitions and existing lockfile.
2. Resolve the target account through STS; enforce `EXPECTED_AWS_ACCOUNT_ID` only
   when it is configured.
3. Render ignored backend configuration, export concrete AWS credentials, apply
   CMA definitions, and resolve agent ID/version/environment ID from the updated
   lock. Bootstrap the state bucket when it does not exist.
4. Build and validate application artifacts, initialize Terraform, and apply a
   saved plan.
5. Sync all supplied deployment secrets to Terraform-created Secrets Manager
   containers, migrate and seed DSQL, upload static assets, invalidate CloudFront,
   and render the Slack manifest.

Set `TF_STATE_BUCKET` to preserve an existing deployment's remote-state bucket.
Otherwise the state bucket is derived from the discovered account, app name, and
region. Terraform outputs, rather than committed documentation, are authoritative
for endpoints and deployed resource identifiers.

## CMA configuration and drift

Preview intended changes with `npm run cma:plan`, then use `./scripts/deploy.sh`
for normal apply/deployment. `npm run cma:show` reads local identity without a
provider call. Initial workspace bootstrap uses an explicit `npm run cma:apply`;
see [adoption and bootstrap](managed-agent-setup.md#adoption-and-bootstrap).

Remote Console/API edits block normal apply. Stop and inspect the change, decide
whether Git or the remote edit reflects intent, and update the source or perform
an explicitly reviewed recovery. Never add automatic force/prune flags. CLI
dry-run can exit successfully with a blocked plan; review its displayed result.

Serialize applies/deployments. Review and commit updated `cma/claude-lock.json`,
including partial applies; deployment warns about dirty lock state but never
commits, discards updates, or deletes old resources. An agent version applied
before an AWS failure remains unselected by previously pinned Lambdas. Environment
changes are ID-based and can affect subsequent sessions before AWS rollout.

For rollback, revert the relevant definition, preview/apply, and redeploy the
resulting agent pin. Environment rollback requires its own reviewed apply and
new-session validation. Webhooks and their secrets remain managed separately.

## First deployment and Slack activation

Use two deliberate stages for a new deployment:

1. Bootstrap CMA lock state, configure Claude/web credentials, leave URL-dependent Slack
   gates disabled, and run `./scripts/deploy.sh`. The post-apply manifest renderer
   uses the newly available Terraform `application_url` output.
2. Import `.generated/slack-manifest.yaml` with Socket Mode disabled. Set
   `PUBLIC_APP_URL` to the `application_url` output, add Slack credentials and
   authorized identity mappings, enable only the intended Slack gates, and rerun
   `./scripts/deploy.sh`. This updates Lambda configuration and synchronizes the
   new secret values.

Do not enable source links or unfurls before the second stage: the initial Lambda
configuration has no public application URL when there was no prior Terraform
output.

## Existing deployment state migration review

For an existing deployment, set its current state bucket in untracked `.env` as
`TF_STATE_BUCKET`. Before a normal deployment, run:

```bash
./scripts/deploy.sh --plan-only
```

This validates configuration and the AWS account, renders the backend, requires
the selected state bucket to already exist, reconfigures Terraform, builds local
artifacts, and saves `dist/application.tfplan`. It does not bootstrap a bucket,
apply Terraform, sync secrets, migrate DSQL, or upload frontend files. It refuses
plans that delete or replace DSQL, S3, CloudFront, API Gateway, Lambda, SQS,
EventBridge, Secrets Manager, or IAM resources. Investigate any such change before
using the normal deployment command.

Plan-only also validates and reads the local CMA lock; it never calls ant apply
or requires CMA connectivity. It uses the current resolved agent version rather
than predicting a version for unapplied source changes.

## Logs and correlation

Handlers emit JSON logs with relevant principal, thread, Task, Slack event,
channel, and webhook identifiers. User message bodies and secret values are not
logged. For Slack A2A failures inspect `slack-ingress`, `agent-input`,
`cma-controller`, `cma-scheduler`, `cma-push`, `a2a-event-sink`, and
`slack-projector` in delivery order. For Web failures inspect `web-api`,
`agui-bridge`, `cma-controller`, and `cma-scheduler` in that order. The browser
uses application thread IDs and the bridge reads canonical A2A Tasks.

The Phase 4 controller and event sink use a private REST API through one
`execute-api` VPC endpoint. The controller route streams through Lambda Web
Adapter; `POST /internal/a2a/events/{agent_id}` is buffered. Terraform's
`private_a2a_url` is authoritative. It must reject requests outside the VPC.
Caller Lambdas use the private subnet and NAT for Slack, Anthropic, and AWS
service calls. Private REST streams have a five-minute idle limit, so a failed
subscription must recover from push notification and `GetTask`.

EventBridge failures enter the application SQS DLQ and raise the
`event-dlq-visible` CloudWatch alarm. The `agent-input`, `cma-scheduler`,
and `cma-push` queues each have a paired DLQ and CloudWatch alarm. The input
queue durably delivers signed Slack work, the scheduler reconciles controller
contexts, and the push queue retries A2A callbacks. Inspect the source event,
Task, and receipt before redriving an item.

## Delivery semantics

- Completed browser and Slack ingress IDs are deduplicated in DSQL.
- A two-minute lease suppresses concurrent workers and permits recovery from
  abandoned attempts.
- Slack projections and live streams use recoverable leases and are recorded only
  after the corresponding Slack API succeeds.
- Provider input and Slack posting cannot be atomically committed with DSQL.
  Logical send IDs and projection receipts reconcile retries; ambiguous provider
  acceptance remains held under KL-003 rather than risking a duplicate input.

## Recovery and administration

- Never manually delete a Terraform lock while an operation is active; use
  `terraform force-unlock` when necessary.
- DSQL deletion protection is enabled. Do not destroy/recreate it as part of a
  portability change.
- Inspect the current Task and projection metadata with
  `uv run --directory backend python ../scripts/live_a2a_inspect.py --team-id T... --channel-id C... --thread-ts 123.456`.
  It prints identities and states only, never transcript text.
- For KL-003, run
  `uv run --directory backend python -m managed_agents_app.cma_controller.admin list-held`
  and `inspect TASK_ID` to identify the held Task and provider send attempt. Inspect
  Anthropic Console or provider evidence for that exact CMA session and input;
  never infer non-acceptance from a missing application projection. If evidence
  proves CMA did not accept it, run `retry TASK_ID --verified-no-side-effect`.
  If it was accepted, reconcile or use `fail TASK_ID` to close the hold safely.
  Record the evidence and action for the operator handoff; do not automatically
  resend an ambiguous external side effect.
- For KL-004, treat a closed A2A subscription as a transport event. Read
  `GetTask`, compare canonical history by stable message ID, and subscribe again
  while the Task remains nonterminal. Push/current-state reconciliation provides
  recovery when the pinned client iterator ends after an agent Message.
- Map a Slack identity observed in a signed event with:

  ```bash
  export DSQL_ENDPOINT="$(terraform -chdir=infra/app output -raw dsql_endpoint)"
  npm run db:map-slack -- --team-id T01234567 --user-id U01234567
  ```
