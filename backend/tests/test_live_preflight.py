from __future__ import annotations

import os
import sys
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import live as cloud


def test_missing_sandbox_credentials_fail_before_network_or_writes(monkeypatch):
    monkeypatch.setattr(cloud, "load_environment", lambda: None)
    monkeypatch.setattr(cloud.shutil, "which", lambda *_: "/bin/tool")
    monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)
    monkeypatch.setattr(cloud, "cli", lambda *_: pytest.fail("network reached before preflight"))
    with pytest.raises(cloud.LiveError, match="SLACK_USER_TOKEN"):
        cloud.preflight(approval=True)


def test_sandbox_account_assertion_fails_before_slack(monkeypatch):
    monkeypatch.setattr(cloud, "load_environment", lambda: None)
    monkeypatch.setattr(cloud.shutil, "which", lambda *_: "/bin/tool")
    for name in ("SLACK_USER_TOKEN", "SLACK_BOT_TOKEN", "E2E_LIVE_SLACK_CHANNEL_ID"):
        monkeypatch.setenv(name, "fixture")
    monkeypatch.setenv("EXPECTED_AWS_ACCOUNT_ID", "expected")
    monkeypatch.setattr(cloud, "cli", lambda *_: {"Account": "other"})
    monkeypatch.setattr(cloud.SlackApi, "auth", lambda *_: pytest.fail("Slack auth attempted"))
    with pytest.raises(cloud.LiveError, match="STS account"):
        cloud.preflight()


@pytest.fixture
def sandbox_boundaries(monkeypatch):
    monkeypatch.setattr(cloud, "load_environment", lambda: None)
    monkeypatch.setattr(cloud.shutil, "which", lambda *_: "/bin/tool")
    for key in (
        "SLACK_USER_TOKEN",
        "SLACK_BOT_TOKEN",
        "E2E_LIVE_SLACK_CHANNEL_ID",
        "SLACK_SIGNING_SECRET",
        "WEB_ACCESS_TOKEN",
    ):
        monkeypatch.setenv(key, key + "-fixture")
    for key in (
        "EXPECTED_AWS_ACCOUNT_ID",
        "E2E_LIVE_SLACK_TEAM_ID",
        "E2E_LIVE_SLACK_USER_ID",
        "SLACK_USER_BOT_ID",
        "SLACK_USER_APP_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    for key in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_REGION",
        "AWS_PROFILE",
        "DSQL_ENDPOINT",
        "E2E_LIVE_BASE_URL",
        "CLAUDE_AGENT_ID",
        "CLAUDE_AGENT_VERSION",
        "CLAUDE_ENVIRONMENT_ID",
    ):
        monkeypatch.setenv(key, "fixture")
    selected = {
        "CLAUDE_AGENT_ID": "agent_fixture",
        "CLAUDE_AGENT_VERSION": "1",
        "CLAUDE_ENVIRONMENT_ID": "env_fixture",
    }
    monkeypatch.setattr(cloud, "deployment_environment", lambda: selected)
    configs = {
        name: {"State": "Active", "LastUpdateStatus": "Successful", "Environment": {"Variables": {}}}
        for name in (*cloud.ORDER, "cma-controller")
    }
    configs["cma-controller"]["Environment"]["Variables"] = dict(selected)
    for name in ("agent-input", "anthropic-webhook", "slack-projector", "cma-controller"):
        configs[name]["Environment"]["Variables"]["DSQL_ENDPOINT"] = "fixture.dsql"
    configs["agent-input"]["Environment"]["Variables"].update(DEV_PRINCIPAL_ID="principal")
    configs["slack-projector"]["Environment"]["Variables"] = {
        "DSQL_ENDPOINT": "fixture.dsql",
    }
    outputs = {
        "application_url": {"value": "https://fixture.example"},
        "dsql_endpoint": {"value": "fixture.dsql"},
    }
    calls = []

    def cli(command):
        calls.append(command)
        if "get-caller-identity" in command:
            return {"Account": "fixture"}
        if "export-credentials" in command:
            return {
                "AccessKeyId": "fixture-access",
                "SecretAccessKey": "fixture-secret",
                "SessionToken": "fixture-session",
            }
        if "output" in command:
            return outputs
        if "show" in command:
            return {
                "values": {
                    "root_module": {
                        "resources": [
                            {
                                "type": "aws_lambda_function",
                                "address": f'aws_lambda_function.app["{name}"]',
                                "values": {"function_name": name},
                            }
                            for name in configs
                        ]
                    }
                }
            }
        return configs[command[-1]]

    monkeypatch.setattr(cloud, "cli", cli)
    api = Mock()
    api.scopes_by_token = {
        "SLACK_USER_TOKEN-fixture": {"chat:write"},
        "SLACK_BOT_TOKEN-fixture": {"chat:write"},
    }
    api.auth.side_effect = lambda token: {
        "team_id": "team",
        "user_id": "human" if token.startswith("SLACK_USER") else "bot",
        **({} if token.startswith("SLACK_USER") else {"bot_id": "bot-identity"}),
    }
    api.call.return_value = {"ok": True, "messages": []}
    monkeypatch.setattr(cloud, "SlackApi", lambda *_: api)
    response = Mock(status_code=200)
    response.json.return_value = {"authenticated": False}
    http = Mock()
    http.get.return_value = response
    http.post.return_value = response
    monkeypatch.setattr(cloud.httpx, "Client", lambda **_: nullcontext(http))
    connection = Mock()
    connection.execute.return_value.fetchone.return_value = {"principal_id": "principal"}
    monkeypatch.setattr(cloud, "connect", lambda *_: nullcontext(connection))
    return configs, outputs, api, http, connection, calls


def test_sandbox_preflight_uses_only_reads_and_exports_credentials_in_memory(sandbox_boundaries):
    _, _, api, _, connection, calls = sandbox_boundaries
    assert cloud.preflight()["ok"]
    assert [call.args[1] for call in api.call.call_args_list] == [
        "conversations.history",
        "conversations.history",
    ]
    assert os.environ["AWS_ACCESS_KEY_ID"] == "fixture-access"
    assert os.environ["DSQL_ENDPOINT"] == "fixture.dsql"
    assert connection.execute.call_count == 2
    assert not any("apply" in call or "sync-secrets" in call or "login" in call for call in calls)


@pytest.mark.parametrize(
    "failure",
    [
        "team",
        "same-user",
        "user-assertion",
        "allowlist",
        "channel",
        "mapping",
        "deploy-state",
        "lock",
        "missing-output",
        "web-auth",
    ],
)
def test_invalid_sandbox_prerequisites_prevent_test_writes(sandbox_boundaries, monkeypatch, failure):
    configs, outputs, api, http, connection, _ = sandbox_boundaries
    if failure == "team":
        api.auth.side_effect = [
            {"team_id": "first", "user_id": "human"},
            {"team_id": "other", "user_id": "bot", "bot_id": "id"},
        ]
    elif failure == "same-user":
        api.auth.side_effect = [
            {"team_id": "team", "user_id": "human"},
            {"team_id": "team", "user_id": "human", "bot_id": "id"},
        ]
    elif failure == "user-assertion":
        monkeypatch.setenv("E2E_LIVE_SLACK_USER_ID", "other")
    elif failure == "allowlist":
        configs["slack-ingress"]["Environment"]["Variables"]["SLACK_USER_ID"] = "other"
    elif failure == "channel":
        api.call.side_effect = cloud.SlackUserError("not_in_channel")
    elif failure == "mapping":
        connection.execute.return_value.fetchone.return_value = None
    elif failure == "deploy-state":
        configs["agent-input"]["State"] = "Failed"
    elif failure == "lock":
        configs["cma-controller"]["Environment"]["Variables"]["CLAUDE_AGENT_ID"] = "drift"
    elif failure == "missing-output":
        outputs.pop("dsql_endpoint")
    elif failure == "web-auth":
        http.post.return_value = Mock(status_code=401)
    with pytest.raises((cloud.LiveError, cloud.SlackUserError)):
        cloud.preflight(approval=True)


def test_missing_installed_scope_fails_before_slack_writes(sandbox_boundaries):
    _, _, api, _, _, _ = sandbox_boundaries
    api.scopes_by_token["SLACK_USER_TOKEN-fixture"] = {"channels:history"}
    with pytest.raises(cloud.LiveError, match="installed chat:write"):
        cloud.preflight(approval=True)


def test_deployed_dsql_drift_prevents_test_writes(sandbox_boundaries):
    configs, _, _, _, _, _ = sandbox_boundaries
    configs["agent-input"]["Environment"]["Variables"]["DSQL_ENDPOINT"] = "wrong-dsql"
    with pytest.raises(cloud.LiveError, match="DSQL endpoint differs"):
        cloud.preflight(approval=True)


def test_deployed_principal_drift_prevents_test_writes(sandbox_boundaries):
    configs, _, _, _, _, _ = sandbox_boundaries
    configs["agent-input"]["Environment"]["Variables"]["DEV_PRINCIPAL_ID"] = "other-principal"
    with pytest.raises(cloud.LiveError, match="not owned"):
        cloud.preflight(approval=True)
