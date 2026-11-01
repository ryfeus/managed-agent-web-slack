from __future__ import annotations

import base64
import os
from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import urlsplit

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict, Field


class AppConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    app_env: Literal["production", "development", "e2e"] = "development"
    database_mode: Literal["dsql", "postgres"] = "dsql"
    database_url: str = ""
    app_name: str
    aws_region: str
    agent_id: str
    environment_id: str
    agent_version: Annotated[int, Field(strict=True, gt=0)] | None = None
    cma_a2a_endpoint: str = ""
    a2a_event_sink_url: str = ""
    cma_a2a_push_allowed_url: str = ""
    cma_scheduler_queue_url: str = ""
    cma_push_queue_url: str = ""
    cma_preview_queue_url: str = ""
    preview_enabled: bool = False
    slack_preview_enabled: bool = False
    slack_preview_queue_url: str = ""
    cma_pending_input_bucket: str = ""
    openappa_binary_path: str = ""
    openappa_policy_path: str = ""
    openappa_timeout_seconds: float = Field(default=10, gt=0, le=30)
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
    slack_user_bot_id: str | None = None
    slack_user_app_id: str | None = None
    public_app_url: str = ""
    slack_receipt_reaction: str = "eyes"


def local_cors_origins(config: AppConfig) -> list[str]:
    origins = ["http://127.0.0.1:3000", "http://localhost:3000"]
    if config.app_env == "e2e" and config.public_app_url:
        url = urlsplit(config.public_app_url)
        if (
            url.scheme != "http"
            or url.hostname not in {"127.0.0.1", "localhost"}
            or url.path
            or url.query
            or url.fragment
            or url.username
            or url.password
        ):
            raise ValueError("E2E frontend origin must be an HTTP loopback origin")
        origins = [config.public_app_url]
    return origins


def _flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _agent_version() -> int | None:
    value = os.getenv("CLAUDE_AGENT_VERSION")
    if value is None:
        return None
    if not value.isascii() or not value.isdecimal() or int(value) < 1:
        raise RuntimeError("CLAUDE_AGENT_VERSION must be a positive integer")
    return int(value)


@lru_cache(maxsize=16)
def _secret(arn_name: str, direct_name: str) -> str | None:
    if os.getenv("APP_ENV") == "e2e":
        return {
            "WEB_ACCESS_TOKEN": "e2e-access-token",
            "WEB_COOKIE_SECRET": "e2e-cookie-secret-at-least-32-bytes",
            "SLACK_SIGNING_SECRET": "e2e-slack-secret",
            "SLACK_BOT_TOKEN": "e2e-bot",
            "ANTHROPIC_API_KEY": "e2e-agent",
            "ANTHROPIC_WEBHOOK_SIGNING_KEY": "whsec_ZTJlLXdlYmhvb2stc2VjcmV0",  # gitleaks:allow local fake
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
        app_name=os.getenv("APP_NAME", "claude-managed-agents-ui-eda"),
        aws_region=os.getenv("AWS_REGION", "us-west-2"),
        agent_id=os.getenv("CLAUDE_AGENT_ID", ""),
        environment_id=os.getenv("CLAUDE_ENVIRONMENT_ID", ""),
        agent_version=_agent_version(),
        cma_a2a_endpoint=os.getenv("CMA_A2A_ENDPOINT", "").strip(),
        a2a_event_sink_url=os.getenv("A2A_EVENT_SINK_URL", "").strip(),
        cma_a2a_push_allowed_url=os.getenv("CMA_A2A_PUSH_ALLOWED_URL", "").strip(),
        cma_scheduler_queue_url=os.getenv("CMA_SCHEDULER_QUEUE_URL", "").strip(),
        cma_push_queue_url=os.getenv("CMA_PUSH_QUEUE_URL", "").strip(),
        cma_preview_queue_url=os.getenv("CMA_PREVIEW_QUEUE_URL", "").strip(),
        preview_enabled=_flag("PREVIEW_ENABLED"),
        slack_preview_enabled=_flag("SLACK_PREVIEW_ENABLED"),
        slack_preview_queue_url=os.getenv("SLACK_PREVIEW_QUEUE_URL", "").strip(),
        cma_pending_input_bucket=os.getenv("CMA_PENDING_INPUT_BUCKET", "").strip(),
        openappa_binary_path=os.getenv("OPENAPPA_BINARY_PATH", ""),
        openappa_policy_path=os.getenv("OPENAPPA_POLICY_PATH", ""),
        openappa_timeout_seconds=float(os.getenv("OPENAPPA_TIMEOUT_SECONDS", "10")),
        dsql_endpoint=os.getenv("DSQL_ENDPOINT", ""),
        dsql_database=os.getenv("DSQL_DATABASE", "postgres"),
        dsql_role=os.getenv("DSQL_ROLE", "app_runtime"),
        event_bus_name=os.getenv("EVENT_BUS_NAME", "claude-managed-agents-dev"),
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
        slack_user_bot_id=os.getenv("SLACK_USER_BOT_ID") or None,
        slack_user_app_id=os.getenv("SLACK_USER_APP_ID") or None,
        public_app_url=os.getenv("PUBLIC_APP_URL", "").rstrip("/"),
        slack_receipt_reaction=os.getenv("SLACK_RECEIPT_REACTION", "eyes").strip() or "eyes",
    )


def is_allowed_by_development_slack_filter(config: AppConfig, team_id: str, user_id: str) -> bool:
    if config.slack_team_allowlist and config.slack_team_allowlist != team_id:
        return False
    return not config.slack_user_allowlist or config.slack_user_allowlist == user_id
