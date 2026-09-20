# Operations

## Deployment order

`scripts/deploy.sh` performs a repeatable real-provider deployment:

1. Load untracked `.env` and validate the required Claude IDs, API key, and web
   credentials.
2. Resolve the target account through STS; enforce `EXPECTED_AWS_ACCOUNT_ID` only
   when it is configured.
3. Render ignored Terraform backend configuration and bootstrap its state bucket
   when it does not exist.
4. Build and validate application artifacts, initialize Terraform, and apply a
   saved plan.
5. Sync all supplied deployment secrets to Terraform-created Secrets Manager
   containers, migrate and seed DSQL, upload static assets, invalidate CloudFront,
   and render the Slack manifest.

Set `TF_STATE_BUCKET` to preserve an existing deployment's remote-state bucket.
Otherwise the state bucket is derived from the discovered account, app name, and
region. Terraform outputs, rather than committed documentation, are authoritative
for endpoints and deployed resource identifiers.

## First deployment and Slack activation

Use two deliberate stages for a new deployment:

1. Configure the required Claude and web credentials, leave URL-dependent Slack
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

For an existing deployment, set its legacy bucket in untracked `.env` as
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

## Logs and correlation

Handlers emit JSON logs with relevant principal, session, Slack event, Managed
Agent event, channel, thread, and webhook identifiers. User message bodies and
secret values are not logged. Inspect `slack-ingress`, `agent-input`,
`anthropic-webhook`, and `slack-projector` in that order.

EventBridge failures enter the application SQS DLQ and raise the
`event-dlq-visible` CloudWatch alarm.

## Delivery semantics

- Completed browser and Slack ingress IDs are deduplicated in DSQL.
- A two-minute lease suppresses concurrent workers and permits recovery from
  abandoned attempts.
- Slack projections and live streams use recoverable leases and are recorded only
  after the corresponding Slack API succeeds.
- Anthropic event submission and Slack posting cannot be atomically committed with
  DSQL; a process failure after a remote side effect and before DSQL commit can
  result in at-least-once duplication.

## Recovery and administration

- Never manually delete a Terraform lock while an operation is active; use
  `terraform force-unlock` when necessary.
- DSQL deletion protection is enabled. Do not destroy/recreate it as part of a
  portability change.
- Map a Slack identity observed in a signed event with:

  ```bash
  export DSQL_ENDPOINT="$(terraform -chdir=infra/app output -raw dsql_endpoint)"
  npm run db:map-slack -- --team-id T01234567 --user-id U01234567
  ```
