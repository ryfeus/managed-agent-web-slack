#!/usr/bin/env python3
"""Explicit API-only live smoke, with read-only account, deployment and identity preflight."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
from dotenv import dotenv_values
from slack_user import SlackApi, SlackUserError

from managed_agents_app.config import AppConfig
from managed_agents_app.db.connection import connect

ROOT = Path(__file__).resolve().parents[1]
ORDER = ("slack-ingress", "agent-input", "anthropic-webhook", "slack-projector")


class LiveError(RuntimeError):
    pass


def load_environment():
    # dotenv parses data; no shell execution. Exported values always win.
    for name, value in dotenv_values(ROOT / ".env").items():
        if value is not None:
            os.environ.setdefault(name, value)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


def cli(command):
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    if result.returncode:
        raise LiveError(
            f"{command[0]} {command[1]} failed (exit {result.returncode}); check credentials and access."
        )
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise LiveError(f"{command[0]} returned invalid JSON") from None


def require(name):
    if not os.getenv(name, "").strip():
        raise LiveError("Missing " + name)
    return os.environ[name]


def resources(tree):
    result = list(tree.get("resources", []))
    for child in tree.get("child_modules", []):
        result.extend(resources(child))
    return result


def deployment_environment():
    from cma_lock import read_lock

    selected = read_lock(ROOT / "cma")
    return {
        key: str(selected[source])
        for key, source in (
            ("CLAUDE_AGENT_ID", "agent_id"),
            ("CLAUDE_AGENT_VERSION", "agent_version"),
            ("CLAUDE_ENVIRONMENT_ID", "environment_id"),
        )
    }


def database_config():
    # Administrative metadata reads must not resolve application/provider secrets.
    return AppConfig(
        app_name="live-preflight",
        aws_region=os.environ["AWS_REGION"],
        agent_id="",
        environment_id="",
        dsql_endpoint=os.environ["DSQL_ENDPOINT"],
        dsql_database="postgres",
        dsql_role="admin",
        event_bus_name="",
        dev_principal_id="",
        web_access_token="",
        web_cookie_secret="",
        anthropic_api_key="",
    )


def preflight(approval=False, metadata_only=False):
    load_environment()
    for tool in ("aws", "terraform"):
        if not shutil.which(tool):
            raise LiveError("Missing system tool: " + tool)
    if not metadata_only:
        for name in ("SLACK_USER_TOKEN", "SLACK_BOT_TOKEN", "E2E_LIVE_SLACK_CHANNEL_ID"):
            require(name)
    if approval:
        for name in ("WEB_ACCESS_TOKEN",):
            require(name)
    profile = os.environ.setdefault("AWS_PROFILE", "default")
    os.environ.setdefault("AWS_REGION", "us-west-2")
    identity = cli(["aws", "sts", "get-caller-identity", "--profile", profile])
    expected = os.getenv("EXPECTED_AWS_ACCOUNT_ID")
    if expected and expected != identity["Account"]:
        raise LiveError("STS account does not match EXPECTED_AWS_ACCOUNT_ID.")
    credentials = cli(["aws", "configure", "export-credentials", "--profile", profile, "--format", "process"])
    if not credentials.get("AccessKeyId") or not credentials.get("SecretAccessKey"):
        raise LiveError("AWS profile did not export concrete credentials.")
    for key, source in (
        ("AWS_ACCESS_KEY_ID", "AccessKeyId"),
        ("AWS_SECRET_ACCESS_KEY", "SecretAccessKey"),
        ("AWS_SESSION_TOKEN", "SessionToken"),
    ):
        if credentials.get(source):
            os.environ[key] = credentials[source]
        else:
            os.environ.pop(key, None)
    outputs = cli(["terraform", "-chdir=infra/app", "output", "-json"])
    for output, variable in (("application_url", "E2E_LIVE_BASE_URL"), ("dsql_endpoint", "DSQL_ENDPOINT")):
        value = outputs.get(output, {}).get("value")
        if not value:
            raise LiveError("Missing Terraform output: " + output)
        os.environ[variable] = value
    state = cli(["terraform", "-chdir=infra/app", "show", "-json"])
    lambdas = {}
    for resource in resources(state.get("values", {}).get("root_module", {})):
        if resource.get("type") == "aws_lambda_function":
            address = resource.get("address", "")
            for name in (*ORDER, "cma-controller"):
                if address.endswith(f'["{name}"]'):
                    lambdas[name] = resource["values"]["function_name"]
    if any(name not in lambdas for name in (*ORDER, "cma-controller")):
        raise LiveError("Terraform state lacks required deployed Lambda identifiers.")
    configs = {}
    for name, function in lambdas.items():
        config = cli(["aws", "lambda", "get-function-configuration", "--function-name", function])
        if config.get("State") != "Active" or config.get("LastUpdateStatus") != "Successful":
            raise LiveError(f"Deployed {name} is not active with a successful update.")
        configs[name] = config.get("Environment", {}).get("Variables", {})
    if metadata_only:
        return {"ok": True, "lambdas": lambdas}
    for name in ("agent-input", "anthropic-webhook", "slack-projector", "cma-controller"):
        if configs[name].get("DSQL_ENDPOINT") != os.environ["DSQL_ENDPOINT"]:
            raise LiveError("Deployed " + name + " DSQL endpoint differs from Terraform.")
    ingress = configs["slack-ingress"]
    with httpx.Client(timeout=20) as client:
        api = SlackApi(client)
        human = api.auth(require("SLACK_USER_TOKEN"))
        bot = api.auth(require("SLACK_BOT_TOKEN"))
        for variable in ("SLACK_USER_TOKEN", "SLACK_BOT_TOKEN"):
            scopes = api.scopes_by_token.get(require(variable))
            if scopes is None or "chat:write" not in scopes:
                raise LiveError(variable + " lacks confirmed installed chat:write scope.")
        if human["team_id"] != bot["team_id"] or human["user_id"] == bot["user_id"] or not bot.get("bot_id"):
            raise LiveError(
                "Slack tokens must authenticate a human and a distinct bot in the same workspace."
            )
        for variable, actual in (
            ("E2E_LIVE_SLACK_TEAM_ID", human["team_id"]),
            ("E2E_LIVE_SLACK_USER_ID", human["user_id"]),
        ):
            if os.getenv(variable) and os.environ[variable] != actual:
                raise LiveError(variable + " does not match the authenticated user token.")
            os.environ[variable] = actual
        channel = require("E2E_LIVE_SLACK_CHANNEL_ID")
        # Both tokens must be able to read the test channel; history is read-only and proves access.
        for token in (require("SLACK_USER_TOKEN"), require("SLACK_BOT_TOKEN")):
            api.call(token, "conversations.history", {"channel": channel, "limit": "1"})
        for setting, actual in (("SLACK_TEAM_ID", human["team_id"]), ("SLACK_USER_ID", human["user_id"])):
            if ingress.get(setting) and ingress[setting] != actual:
                raise LiveError("Deployed ingress allowlist rejects the authenticated Slack identity.")
        for key in ("SLACK_USER_BOT_ID", "SLACK_USER_APP_ID"):
            if bool(ingress.get(key)) != bool(os.getenv(key)) or (
                os.getenv(key) and ingress.get(key) != os.environ[key]
            ):
                raise LiveError("Deployed exact user-token metadata pair differs from local " + key)
        response = client.get(os.environ["E2E_LIVE_BASE_URL"] + "/api/auth/session")
        if response.status_code not in (200, 401) or "authenticated" not in response.json():
            raise LiveError("Deployed web API is unavailable.")
        if approval:
            response = client.post(
                os.environ["E2E_LIVE_BASE_URL"] + "/api/auth/session",
                json={"accessToken": require("WEB_ACCESS_TOKEN")},
            )
            if response.status_code != 200:
                raise LiveError(
                    "Configured WEB_ACCESS_TOKEN cannot authenticate against the deployed web API."
                )
    # Identity verification needs only DSQL, not provider secrets or lock selection.
    with connect(database_config(), "admin") as conn:
        row = conn.execute(
            "SELECT principal_id FROM external_identities WHERE provider = %s "
            "AND tenant_id = %s AND external_user_id = %s",
            ("slack", human["team_id"], human["user_id"]),
        ).fetchone()
        web = conn.execute(
            "SELECT principal_id FROM external_identities WHERE provider = %s "
            "AND tenant_id = %s AND external_user_id = %s",
            ("web", "development", "single-user"),
        ).fetchone()
        principal = configs["agent-input"].get("DEV_PRINCIPAL_ID")
        if (
            not row
            or not web
            or str(row["principal_id"]) != str(web["principal_id"])
            or str(row["principal_id"]) != principal
        ):
            raise LiveError("Slack identity is missing or not owned by the configured web principal in DSQL.")
    # Focused suite selects deployed lock identity; validate drift without applying definitions.
    selected = deployment_environment()
    for key, value in selected.items():
        os.environ[key] = str(value)
        if key.startswith("CLAUDE_") and configs["cma-controller"].get(key) != str(value):
            raise LiveError("Deployed controller differs from lock-derived " + key)
    return {
        "ok": True,
        "team_id": human["team_id"],
        "user_id": human["user_id"],
        "channel": channel,
        "principal_id": str(row["principal_id"]),
        "base_url": os.environ["E2E_LIVE_BASE_URL"],
        "lambdas": lambdas,
        "account_assertion": "matched" if expected else "not-configured",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", choices=("smoke", "approval", "all", "demos"), default="all")
    args = parser.parse_args()
    if os.getenv("RUN_LIVE_E2E") != "1":
        print("Live smoke sends real messages. Set RUN_LIVE_E2E=1 to opt in.", file=sys.stderr)
        return 1
    try:
        preflight(approval=args.mode != "smoke")
        if args.mode in ("smoke", "all"):
            subprocess.run(
                [sys.executable, str(ROOT / "scripts/slack_user.py"), "smoke"], cwd=ROOT, check=True
            )
        if args.mode == "demos":
            subprocess.run(
                [
                    "node",
                    "node_modules/@playwright/test/cli.js",
                    "test",
                    "--config=playwright.live.config.ts",
                    "policy-demos.spec.ts",
                ],
                cwd=ROOT,
                check=True,
            )
        if args.mode in ("approval", "all"):
            subprocess.run(
                [
                    "node",
                    "node_modules/@playwright/test/cli.js",
                    "test",
                    "--config=playwright.live.config.ts",
                    "slack-approval.spec.ts",
                    *(["slack-clarification.spec.ts", "custom-tools.spec.ts"] if args.mode == "all" else []),
                ],
                cwd=ROOT,
                check=True,
            )
        return 0
    except (LiveError, SlackUserError) as error:
        print(str(error), file=sys.stderr)
    except Exception as error:
        print("Live boundary failed: " + type(error).__name__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
