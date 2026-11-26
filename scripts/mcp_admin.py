"""Explicit MCP DSQL grants and verified Cognito linking. Never reads dotenv or passwords."""

from __future__ import annotations

import argparse
import re
import uuid
from typing import Any

import aurora_dsql_psycopg as dsql
import boto3


def link_identity(conn: Any, issuer: str, subject: str, principal_id: str) -> None:
    principal_id = str(uuid.UUID(principal_id))
    with conn.cursor() as cur:
        cur.execute("SELECT principal_id FROM principals WHERE principal_id = %s", (principal_id,))
        if cur.fetchone() is None:
            raise ValueError("Principal must already exist")
        cur.execute(
            "INSERT INTO external_identities "
            "(identity_id, principal_id, provider, tenant_id, external_user_id) "
            "VALUES (%s, %s, 'cognito', %s, %s) "
            "ON CONFLICT (provider, tenant_id, external_user_id) DO NOTHING",
            (str(uuid.uuid4()), principal_id, issuer, subject),
        )
        cur.execute(
            "SELECT principal_id FROM external_identities "
            "WHERE provider = 'cognito' AND tenant_id = %s AND external_user_id = %s",
            (issuer, subject),
        )
        row = cur.fetchone()
        if row is None or str(row[0]) != principal_id:
            raise ValueError("Identity is already linked to another principal; refusing reassignment")


def grant_role(conn: Any, arn: str, account: str) -> None:
    if not re.fullmatch(rf"arn:aws:iam::{re.escape(account)}:role/[A-Za-z0-9+=,.@_/-]+", arn):
        raise ValueError("Role ARN must belong to the verified AWS account")
    conn.autocommit = True
    with conn.cursor() as cur:
        try:
            cur.execute("CREATE ROLE mcp_runtime WITH LOGIN")
        except Exception as error:
            if "already exists" not in str(error).lower():
                raise
        cur.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM mcp_runtime")
        # DSQL's system-owned public schema already permits usage and rejects
        # schema grants. Authorize only these application tables, as in migrations.
        cur.execute("GRANT SELECT ON external_identities TO mcp_runtime")
        cur.execute("GRANT SELECT, INSERT, UPDATE ON agent_threads, agent_tasks TO mcp_runtime")
        try:
            cur.execute(f"AWS IAM GRANT mcp_runtime TO '{arn}'")
        except Exception as error:
            if "already" not in str(error).lower():
                raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--expected-account-id")
    sub = parser.add_subparsers(dest="command", required=True)
    grant = sub.add_parser("grant-role")
    grant.add_argument("--role-arn", required=True)
    link = sub.add_parser("link-user")
    link.add_argument("--user-pool-id", required=True)
    link.add_argument("--username", required=True)
    link.add_argument("--principal-id", required=True)
    args = parser.parse_args()
    account = boto3.client("sts", region_name=args.region).get_caller_identity()["Account"]
    if args.expected_account_id and args.expected_account_id != account:
        raise ValueError("AWS account does not match the expected account")
    with dsql.connect(
        host=args.endpoint, region=args.region, user="admin", dbname="postgres", sslmode="require"
    ) as conn:
        if args.command == "grant-role":
            grant_role(conn, args.role_arn, account)
        else:
            if not re.fullmatch(rf"{re.escape(args.region)}_[A-Za-z0-9]+", args.user_pool_id):
                raise ValueError("User pool must belong to the selected region")
            user = boto3.client("cognito-idp", region_name=args.region).admin_get_user(
                UserPoolId=args.user_pool_id, Username=args.username
            )
            if not user.get("Enabled"):
                raise ValueError("Cognito user is disabled")
            subject = next(item["Value"] for item in user["UserAttributes"] if item["Name"] == "sub")
            issuer = f"https://cognito-idp.{args.region}.amazonaws.com/{args.user_pool_id}"
            link_identity(conn, issuer, subject, args.principal_id)
    print(f"Completed MCP {args.command} in AWS account {account}.")


if __name__ == "__main__":
    main()
