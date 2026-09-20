# Aurora DSQL

## Remote state and cluster

Terraform renders deployment-specific backend configuration to
`.generated/terraform/backend.hcl`. It uses an encrypted, versioned S3 bucket with
blocked public access, bucket-owner enforcement, and S3 native lockfiles. The
default bucket name derives from the discovered account, app name, and region;
`TF_STATE_BUCKET` supports an existing shared bucket.

The application provisions one deletion-protected DSQL cluster in the selected
supported region. Use `AWS_REGION=us-west-2` unless the stack has been separately
validated in another region.

## Runtime authentication

Migrations connect as `admin` with `dsql:DbConnectAdmin`. They create
`app_runtime`, map it to applicable Lambda IAM roles with `AWS IAM GRANT`, and
grant table DML privileges. Deployed Lambdas retain only `dsql:DbConnect`, never
`DbConnectAdmin`.

The Aurora DSQL Psycopg connector generates IAM authentication tokens and requires
TLS with `sslmode=require`; no database password exists.

## Schema

The schema contains principals, external identity mappings, session authorization,
Slack bindings, ingress state/leases, projection receipts, response-stream leases,
feedback metadata, and migration versions. It contains no Claude or Slack
transcript. `agent_feedback` stores identifiers and ratings, while
`slack_response_streams` stores cursors, timestamps, attempts, leases, and errors
without message content.

## Commands

```bash
export AWS_PROFILE=default
export AWS_REGION=us-west-2
export AWS_SDK_LOAD_CONFIG=1
eval "$(aws configure export-credentials --profile default --format env)"

npm run db:migrate
npm run db:seed
npm run db:map-slack -- --team-id T01234567 --user-id U01234567
```

`db:map-slack` maps the verified workspace and human member IDs from a signed
Slack event to an application principal. It does not make optional development
allowlists into authorization credentials.
