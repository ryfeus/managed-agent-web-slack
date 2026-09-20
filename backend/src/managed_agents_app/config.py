from __future__ import annotations

import base64
import os
from functools import lru_cache
from typing import Literal

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict


class AppConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    app_env: Literal["production", "development", "e2e"] = "development"
    database_mode: Literal["dsql", "postgres"] = "dsql"
    database_url: str = ""
    app_name: str
    aws_region: str
    agent_id: str
    environment_id: str
    dsql_endpoint: str
    dsql_database: str
    dsql_role: str
    event_bus_name: str
    dev_principal_id: str
    web_access_token: str
    web_cookie_secret: str
    anthropic_api_key: str
    anthropic_webhook_signing_key: str | None = None
    slack_signing_secret: str | None = None
    slack_bot_token: str | None = None
    slack_team_allowlist: str | None = None
    slack_user_allowlist: str | None = None
    public_app_url: str = ""
    slack_bound_thread_replies: bool = False
    slack_agent_view_enabled: bool = False
    slack_streaming_enabled: bool = False
    slack_tool_approvals_enabled: bool = False
    slack_feedback_enabled: bool = False
    slack_shortcuts_enabled: bool = False
    slack_unfurls_enabled: bool = False
    slack_active_context_enabled: bool = False
    slack_receipt_reaction_enabled: bool = False
    slack_receipt_reaction: str = "eyes"
    slack_source_links_enabled: bool = False
    slack_task_cards_enabled: bool = False


def _flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=16)
def _secret(arn_name: str, direct_name: str) -> str | None:
    if os.getenv("APP_ENV") == "e2e":
        return {
            "WEB_ACCESS_TOKEN": "e2e-access-token",
            "WEB_COOKIE_SECRET": "e2e-cookie-secret-at-least-32-bytes",
            "SLACK_SIGNING_SECRET": "e2e-slack-secret",
            "SLACK_BOT_TOKEN": "e2e-bot",
            "ANTHROPIC_API_KEY": "e2e-agent",
            "ANTHROPIC_WEBHOOK_SIGNING_KEY": "whsec_ZTJlLXdlYmhvb2stc2VjcmV0",
        }.get(direct_name)
    direct = os.getenv(direct_name)
    if direct:
        return direct
    arn = os.getenv(arn_name)
    if not arn:
        return None
    try:
        result = boto3.client("secretsmanager").get_secret_value(SecretId=arn)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return None
        raise
    if value := result.get("SecretString"):
        return str(value)
    binary = result.get("SecretBinary")
    return base64.b64decode(binary).decode() if binary else None


@lru_cache(maxsize=1)
def load_config() -> AppConfig:
    return AppConfig(
        app_env=os.getenv("APP_ENV", "development"),
        database_mode=os.getenv("DATABASE_MODE", "dsql"),
        database_url=os.getenv("DATABASE_URL", ""),
        app_name=os.getenv("APP_NAME", "managed-agent-web-slack"),
        aws_region=os.getenv("AWS_REGION", "us-west-2"),
        agent_id=os.getenv("CLAUDE_AGENT_ID") or os.getenv("AGENT_ID", ""),
        environment_id=os.getenv("CLAUDE_ENVIRONMENT_ID", ""),
        dsql_endpoint=os.getenv("DSQL_ENDPOINT", ""),
        dsql_database=os.getenv("DSQL_DATABASE", "postgres"),
        dsql_role=os.getenv("DSQL_ROLE", "app_runtime"),
        event_bus_name=os.getenv("EVENT_BUS_NAME", "managed-agent-web-slack-dev"),
        dev_principal_id=os.getenv("DEV_PRINCIPAL_ID", "00000000-0000-4000-8000-000000000001"),
        web_access_token=_secret("WEB_ACCESS_TOKEN_SECRET_ARN", "WEB_ACCESS_TOKEN") or "",
        web_cookie_secret=_secret("WEB_COOKIE_SECRET_SECRET_ARN", "WEB_COOKIE_SECRET") or "",
        anthropic_api_key=_secret("ANTHROPIC_API_KEY_SECRET_ARN", "ANTHROPIC_API_KEY") or "",
        anthropic_webhook_signing_key=_secret(
            "ANTHROPIC_WEBHOOK_SIGNING_KEY_SECRET_ARN", "ANTHROPIC_WEBHOOK_SIGNING_KEY"
        ),
        slack_signing_secret=_secret("SLACK_SIGNING_SECRET_SECRET_ARN", "SLACK_SIGNING_SECRET"),
        slack_bot_token=_secret("SLACK_BOT_TOKEN_SECRET_ARN", "SLACK_BOT_TOKEN"),
        slack_team_allowlist=os.getenv("SLACK_TEAM_ID") or None,
        slack_user_allowlist=os.getenv("SLACK_USER_ID") or None,
        public_app_url=os.getenv("PUBLIC_APP_URL", "").rstrip("/"),
        slack_bound_thread_replies=_flag("SLACK_BOUND_THREAD_REPLIES"),
        slack_agent_view_enabled=_flag("SLACK_AGENT_VIEW_ENABLED"),
        slack_streaming_enabled=_flag("SLACK_STREAMING_ENABLED"),
        slack_tool_approvals_enabled=_flag("SLACK_TOOL_APPROVALS_ENABLED"),
        slack_feedback_enabled=_flag("SLACK_FEEDBACK_ENABLED"),
        slack_shortcuts_enabled=_flag("SLACK_SHORTCUTS_ENABLED"),
        slack_unfurls_enabled=_flag("SLACK_UNFURLS_ENABLED"),
        slack_active_context_enabled=_flag("SLACK_ACTIVE_CONTEXT_ENABLED"),
        slack_receipt_reaction_enabled=_flag("SLACK_RECEIPT_REACTION_ENABLED"),
        slack_receipt_reaction=os.getenv("SLACK_RECEIPT_REACTION", "eyes").strip() or "eyes",
        slack_source_links_enabled=_flag("SLACK_SOURCE_LINKS_ENABLED"),
        slack_task_cards_enabled=_flag("SLACK_TASK_CARDS_ENABLED"),
    )


def is_allowed_by_development_slack_filter(config: AppConfig, team_id: str, user_id: str) -> bool:
    if config.slack_team_allowlist and config.slack_team_allowlist != team_id:
        return False
    return not config.slack_user_allowlist or config.slack_user_allowlist == user_id
