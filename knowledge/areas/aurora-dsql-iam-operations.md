---
type: Runbook
title: "Aurora DSQL IAM Operations"
description: "Use Aurora DSQL with temporary IAM authentication, least-privilege database roles, idempotent mappings, and optimistic-concurrency retries."
tags:
  - aws
  - aurora-dsql
  - iam
  - postgresql
status: stable
aliases:
  - DSQL IAM operations
sources:
  - id: dsql-authentication-authorization
    resource: "https://docs.aws.amazon.com/aurora-dsql/latest/userguide/authentication-authorization.html"
    title: "Authentication and authorization for Aurora DSQL"
  - id: dsql-database-iam-roles
    resource: "https://docs.aws.amazon.com/aurora-dsql/latest/userguide/using-database-and-iam-roles.html"
    title: "Using database roles and IAM authentication"
  - id: dsql-concurrency-control
    resource: "https://docs.aws.amazon.com/aurora-dsql/latest/userguide/working-with-concurrency-control.html"
    title: "Concurrency control in Aurora DSQL"
  - id: dsql-client-access
    resource: "https://docs.aws.amazon.com/aurora-dsql/latest/userguide/accessing.html"
    title: "Accessing Aurora DSQL with PostgreSQL-compatible clients"
  - id: local-database-adapter
    resource: "../../backend/src/managed_agents_app/db/repositories.py"
    title: "Application database adapter"
generated:
  by: openai-codex/gpt-5
  at: 2026-09-03T04:34:14Z
---

# Aurora DSQL IAM Operations

## Authentication and authorization layers

Aurora DSQL combines IAM connection authorization with PostgreSQL database roles and privileges.

- `dsql:DbConnectAdmin` permits an IAM identity to connect as the predefined `admin` database role.
- `dsql:DbConnect` permits an IAM identity to connect through a custom database role.
- PostgreSQL grants determine what the connected database role may do after authentication.
- `AWS IAM GRANT role TO 'iam-arn'` associates a custom DSQL database role with an IAM role or user.

Use `admin` only for schema migration, database-role management, and IAM association. Application Lambdas should connect through a custom least-privilege role such as `app_runtime`.

## Temporary credentials

DSQL connections authenticate with temporary IAM-derived tokens. Local SSO, Terraform, AWS CLI, and Node SDK clients do not always resolve profiles identically.

For local administration:

1. Verify `aws sts get-caller-identity --profile default` against the expected account.
2. Refresh SSO if it fails.
3. Export concrete temporary credentials with `aws configure export-credentials`.
4. Set `AWS_REGION` to the DSQL cluster region.
5. Set `DSQL_ENDPOINT` from the Terraform output.
6. Connect as `admin` only for the scoped administrative operation.

Do not store generated database authentication tokens in `.env`. Existing connections have a bounded authorization lifetime, and clients must generate fresh authentication material when opening new connections.

## Application data boundary

For a Managed Agents adapter, DSQL should store:

- Application principals.
- External identities such as `(provider, tenant_id, external_user_id)`.
- Authorized Managed Agent session ownership.
- Slack channel/thread bindings.
- Ingress idempotency claims and outcomes.
- Projection claims, leases, and receipts.

Do not store Claude message content, reasoning, tool calls, or transcript copies. Those belong to the Managed Agent event log.

## Identity mapping

Create a Slack identity mapping only after an authentic signed Slack event supplies the workspace and human user IDs. The mapping operation should be idempotent:

```sql
INSERT INTO external_identities
  (identity_id, principal_id, provider, tenant_id, external_user_id)
VALUES
  (..., ..., 'slack', ..., ...)
ON CONFLICT (provider, tenant_id, external_user_id)
DO UPDATE SET principal_id = EXCLUDED.principal_id;
```

Environment allowlists may reject unwanted development traffic before the lookup, but they must never substitute for a database mapping.

## Optimistic concurrency

Aurora DSQL uses optimistic concurrency control. Conflicts surface at commit as PostgreSQL SQLSTATE `40001`, including data conflicts and schema-catalog conflicts.

Design writes so the entire transaction can be retried safely:

- Use random UUID primary keys to distribute writes.
- Put unique constraints on business idempotency keys.
- Keep transactions short.
- Avoid high-contention singleton rows.
- Retry `40001` with bounded exponential backoff and jitter.
- Rerun the whole transaction callback, not only the last SQL statement.
- Ensure side effects outside DSQL occur only after a durable claim or have their own idempotency key.

Do not assume `SELECT FOR UPDATE` provides traditional blocking-lock behavior; conflicts may still be detected optimistically at commit.

## Migration and role workflow

1. Connect as `admin` with an IAM principal allowed to use `dsql:DbConnectAdmin`.
2. Create or update application tables and the custom runtime database role.
3. Grant only required table privileges to the runtime role.
4. Associate each Lambda execution-role ARN with the runtime database role using `AWS IAM GRANT`.
5. Give those Lambda roles `dsql:DbConnect`, not admin connection permission.
6. Run migrations idempotently and record applied migration files.

The repository's `scripts/deploy.sh` obtains the DSQL endpoint and Lambda role ARNs from Terraform before running migration and seed commands.

## Troubleshooting

- **Missing DSQL endpoint:** export `terraform -chdir=infra/app output -raw dsql_endpoint` before standalone database scripts.
- **Token or caller-identity error:** refresh SSO and export concrete credentials; verify region and account.
- **Database user not found:** confirm the custom role exists and its IAM association is present.
- **Permission denied:** separate IAM connection permission from PostgreSQL object privileges.
- **Intermittent `40001`:** retry the full idempotent transaction with backoff.
- **Migration `40001`:** retry after concurrent schema changes settle; DDL can invalidate cached schema catalogs.
- **Lambda works but local script fails:** compare endpoint, region, database role, and credential provider chain.

## Related knowledge

- [Slack Events API operations](slack-events-api-operations.md)
- [Anthropic Managed Agents operations](anthropic-managed-agents-operations.md)
- [Managed Agent application end-to-end runbook](../projects/managed-agent-application-e2e-runbook.md)
