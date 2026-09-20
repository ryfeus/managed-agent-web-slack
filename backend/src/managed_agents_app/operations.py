from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import boto3
from dotenv import load_dotenv

from managed_agents_app.config import AppConfig, load_config
from managed_agents_app.db import Database
from managed_agents_app.db.connection import connect

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = REPOSITORY_ROOT / "backend" / "migrations"


def main() -> None:
    if os.getenv("APP_ENV") != "e2e":
        load_dotenv(REPOSITORY_ROOT / ".env")
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("migrate")
    subcommands.add_parser("seed")
    mapping = subcommands.add_parser("map-slack")
    mapping.add_argument("--team-id", required=True)
    mapping.add_argument("--user-id", required=True)
    mapping.add_argument("--principal-id")
    subcommands.add_parser("sync-secrets")
    args = parser.parse_args()
    load_config.cache_clear()
    if args.command == "migrate":
        migrate()
    elif args.command == "seed":
        seed()
    elif args.command == "map-slack":
        map_slack(args.team_id, args.user_id, args.principal_id)
    else:
        sync_secrets()


def migrate() -> None:
    config = load_config()
    _execute_admin(
        config,
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)",
    )
    for path in sorted(MIGRATIONS.glob("*.sql")):
        with connect(config, "admin") as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM schema_migrations WHERE version = %s LIMIT 1", (path.name,))
            if cur.fetchone():
                continue

        # Aurora DSQL permits only one DDL statement in a transaction. The
        # migrations use idempotent DDL so a partially applied file is safe to
        # rerun before its version marker is written.
        for statement in _statements(path.read_text()):
            _execute_admin(config, statement)
        _execute_admin(
            config,
            "INSERT INTO schema_migrations (version) VALUES (%s)",
            (path.name,),
        )
        print(f"Applied {path.name}")

    if config.database_mode == "postgres":
        return
    role = _identifier(config.dsql_role)
    with connect(config, "admin") as conn, conn.cursor() as cur:
        conn.autocommit = True
        try:
            cur.execute(f"CREATE ROLE {role} WITH LOGIN")
        except Exception as error:
            if "already exists" not in str(error).lower():
                raise
        role_arns = [
            value.strip() for value in os.getenv("DSQL_RUNTIME_ROLE_ARNS", "").split(",") if value.strip()
        ]
        for arn in role_arns:
            if not re.fullmatch(r"arn:aws:iam::\d{12}:role/[A-Za-z0-9+=,.@_/-]+", arn):
                raise ValueError(f"Invalid IAM role ARN: {arn}")
            try:
                escaped_arn = arn.replace("'", "''")
                cur.execute(f"AWS IAM GRANT {role} TO '{escaped_arn}'")
            except Exception as error:
                if "already" not in str(error).lower():
                    raise
        cur.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}")


def seed() -> None:
    config = load_config()
    db = Database(config, role="admin")
    db.ensure_principal(config.dev_principal_id)
    db.map_external_identity(config.dev_principal_id, "web", "development", "single-user")
    if config.slack_team_allowlist and config.slack_user_allowlist:
        db.map_external_identity(
            config.dev_principal_id,
            "slack",
            config.slack_team_allowlist,
            config.slack_user_allowlist,
        )
        print("Seeded the optional development Slack identity mapping.")
    else:
        print("No Slack allowlist pair configured; add Slack identities through DSQL administration.")


def map_slack(team_id: str, user_id: str, principal_id: str | None) -> None:
    if not re.fullmatch(r"T[A-Z0-9]+", team_id):
        raise ValueError("--team-id must be a Slack team ID such as T01234567")
    if not re.fullmatch(r"[UW][A-Z0-9]+", user_id):
        raise ValueError("--user-id must be a human Slack member ID such as U01234567")
    config = load_config()
    target = principal_id or config.dev_principal_id
    if not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", target, re.I
    ):
        raise ValueError("--principal-id must be a UUID")
    db = Database(config, role="admin")
    db.ensure_principal(target)
    db.map_external_identity(target, "slack", team_id, user_id)
    print(f"Mapped Slack {team_id}/{user_id} to principal {target}.")


def sync_secrets() -> None:
    outputs: dict[str, dict[str, Any]] = json.loads(
        subprocess.check_output(["terraform", "-chdir=infra/app", "output", "-json"], cwd=REPOSITORY_ROOT)
    )
    mappings = {
        "anthropic_api_key_secret_arn": "ANTHROPIC_API_KEY",
        "anthropic_webhook_signing_key_secret_arn": "ANTHROPIC_WEBHOOK_SIGNING_KEY",
        "slack_signing_secret_arn": "SLACK_SIGNING_SECRET",
        "slack_bot_token_secret_arn": "SLACK_BOT_TOKEN",
        "web_access_token_secret_arn": "WEB_ACCESS_TOKEN",
        "web_cookie_secret_secret_arn": "WEB_COOKIE_SECRET",
    }
    for output_name, environment_name in mappings.items():
        secret_id = outputs.get(output_name, {}).get("value")
        value = os.getenv(environment_name)
        if not secret_id or not value:
            print(f"Skipping {environment_name}; value or Terraform output is unavailable.")
            continue
        region = str(secret_id).split(":")[3]
        boto3.client("secretsmanager", region_name=region).put_secret_value(
            SecretId=secret_id, SecretString=value
        )
        print(f"Updated {environment_name} secret.")


def _statements(source: str) -> list[str]:
    return [statement.strip() for statement in re.split(r";\s*(?:\n|$)", source) if statement.strip()]


def _execute_admin(config: AppConfig, statement: str, parameters: tuple[object, ...] = ()) -> None:
    """Execute exactly one statement in its own DSQL transaction."""
    with connect(config, "admin") as conn, conn.cursor() as cur:
        cur.execute(statement, parameters)


def _identifier(value: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", value):
        raise ValueError(f"Invalid SQL identifier: {value}")
    return value


if __name__ == "__main__":
    main()
