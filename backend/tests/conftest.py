from __future__ import annotations

import pytest

from managed_agents_app.config import AppConfig


@pytest.fixture
def config() -> AppConfig:
    return AppConfig(
        app_name="test-app",
        aws_region="us-west-2",
        agent_id="agent_test",
        environment_id="env_test",
        dsql_endpoint="example.dsql.us-west-2.on.aws",
        dsql_database="postgres",
        dsql_role="app_runtime",
        event_bus_name="test-events",
        dev_principal_id="00000000-0000-4000-8000-000000000001",
        web_access_token="access-token",
        web_cookie_secret="cookie-secret",
        anthropic_api_key="anthropic-key",
        anthropic_webhook_signing_key="webhook-key",
        slack_signing_secret="slack-secret",
        slack_bot_token="xoxb-test",
    )


@pytest.fixture
def runtime(monkeypatch, config):
    from unittest.mock import MagicMock

    from managed_agents_app.handlers import (
        agent_input,
        anthropic_webhook,
        slack_ingress,
        slack_projector,
        web_api,
    )
    from managed_agents_app.runtime import Runtime

    result = Runtime(config, MagicMock(), MagicMock(), MagicMock(), MagicMock())

    def factory(config):
        result.config = config
        return result

    for module in [agent_input, slack_projector, slack_ingress, anthropic_webhook, web_api]:
        monkeypatch.setattr(module, "get_runtime", factory)
    return result
