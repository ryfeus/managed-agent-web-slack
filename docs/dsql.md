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
TLS with `sslmode=require`; no database password exists. The TypeScript Web Function uses the official Aurora DSQL postgres.js connector with certificate validation. Its new IAM role must be granted the existing `app_runtime` database role before cutover; Terraform IAM `dsql:DbConnect` alone is insufficient. Phase 5 adds no tables, columns or transcript store.

The Phase 6 Slack preview execution role also needs an explicit `AWS IAM GRANT app_runtime` before enabling its queue mapping. Use Terraform's `dsql_runtime_role_arns` output as the role source. IAM `dsql:DbConnect` alone does not authorize the database role. This worker shares the existing projection claim fields and adds no tables or columns. The shared `app_runtime` role retains broad application DML; the small worker repository limits itself to metadata reads and fenced projection claims, not a separate SQL security boundary.

## Schema

The schema contains principals, external identity mappings, application threads,
surface bindings, A2A Task bindings, ingress state, projection receipts, controller
contexts/Tasks, review digests/rulings and push state. Historical feedback metadata
is retained but no longer written by the product. It contains no Claude or Slack transcript;
pending human input is referenced only by opaque temporary-object keys.

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

See the [DSQL IAM runbook](../knowledge/areas/aurora-dsql-iam-operations.md) for roles, TLS and transaction retries.
