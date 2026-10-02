import importlib
from threading import Barrier

import pytest

from managed_agents_app.runtime import get_runtime
from managed_agents_app.testing.local_event_bus import LocalEventBus
from managed_agents_app.testing.recording_slack import RecordingSlack


def test_e2e_configuration_never_resolves_secrets(monkeypatch):
    from managed_agents_app.config import _secret, load_config

    monkeypatch.setenv("APP_ENV", "e2e")
    monkeypatch.setenv("CLAUDE_AGENT_VERSION", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY_SECRET_ARN", "must-not-read")
    monkeypatch.setenv("SLACK_USER_BOT_ID", "B_USER")
    monkeypatch.setenv("SLACK_USER_APP_ID", "A_USER")
    monkeypatch.setattr("managed_agents_app.config.boto3.client", lambda *_: pytest.fail("AWS contacted"))
    _secret.cache_clear()
    load_config.cache_clear()
    try:
        config = load_config()
        assert config.anthropic_api_key == "e2e-agent"
        assert config.agent_version == 1
        assert config.web_access_token == "e2e-access-token"
        assert config.web_cookie_secret == "e2e-cookie-secret-at-least-32-bytes"
        assert config.slack_user_bot_id == "B_USER"
        assert config.slack_user_app_id == "A_USER"
    finally:
        _secret.cache_clear()
        load_config.cache_clear()


def test_production_web_secrets_resolve_from_secret_references(monkeypatch):
    from managed_agents_app.config import _secret, load_config

    class SecretsManager:
        def get_secret_value(self, *, SecretId):
            return {"SecretString": {"web-token": "token", "cookie-secret": "cookie"}[SecretId]}

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("WEB_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("WEB_COOKIE_SECRET", raising=False)
    monkeypatch.setenv("WEB_ACCESS_TOKEN_SECRET_ARN", "web-token")
    monkeypatch.setenv("WEB_COOKIE_SECRET_SECRET_ARN", "cookie-secret")
    monkeypatch.setattr("managed_agents_app.config.boto3.client", lambda *_: SecretsManager())
    _secret.cache_clear()
    load_config.cache_clear()
    try:
        config = load_config()
        assert config.web_access_token == "token"
        assert config.web_cookie_secret == "cookie"
    finally:
        _secret.cache_clear()
        load_config.cache_clear()


def test_runtime_modes(config):
    from managed_agents_app.local_api import create_app

    development = get_runtime(config)
    assert development.config.app_env == "development"
    assert "agent" not in vars(development)  # no eager network SDK construction
    assert all(
        not getattr(route, "path", "").startswith("/_test") for route in create_app(development).routes
    )
    e2e = config.model_copy(
        update={
            "app_env": "e2e",
            "database_mode": "postgres",
            "database_url": "postgresql://localhost/managed_agents_e2e",
        }
    )
    local = get_runtime(e2e)
    assert local is get_runtime(e2e)
    assert not hasattr(local, "agent")
    assert isinstance(local.slack, RecordingSlack)


@pytest.mark.parametrize(
    "module_name, callable_name, arguments",
    [
        ("managed_agents_app.handlers.slack_ingress", "handler", ({}, None)),
        ("managed_agents_app.handlers.agent_input_sqs", "handler", ({}, None)),
        ("managed_agents_app.cma_controller.webhook", "handler", ({}, None)),
        ("managed_agents_app.handlers.slack_projector", "handler", ({}, None)),
        ("managed_agents_app.handlers.web_api", "handler", ({}, None)),
        ("managed_agents_app.agui_bridge.app", "production_app", ()),
    ],
)
def test_production_entrypoints_reject_e2e_runtime(
    config, monkeypatch, module_name, callable_name, arguments
):
    module = importlib.import_module(module_name)
    e2e = config.model_copy(update={"app_env": "e2e"})
    monkeypatch.setattr(module, "load_config", lambda: e2e)
    with pytest.raises(RuntimeError, match="local_api"):
        getattr(module, callable_name)(*arguments)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://production/managed_agents_e2e",
        "postgresql://localhost/postgres",
        "postgresql://localhost/managed_agents_e2e?host=production",
        "https://localhost/managed_agents_e2e",
    ],
)
def test_reset_database_guard(config, url):
    from managed_agents_app.testing.factory import validate_local_database

    with pytest.raises(ValueError):
        validate_local_database(
            config.model_copy(update={"app_env": "e2e", "database_mode": "postgres", "database_url": url})
        )


def test_queue_failure_retry_duplicate_and_bounded_drain():
    bus = LocalEventBus()
    received = []
    bus.dispatch = lambda event: received.append(event["detail"])
    bus.failures["A"] = 1
    bus.publish("test", "A", {"id": 1})
    bus.publish("test", "B", {"id": 2})
    result = bus.drain()
    assert received == [{"id": 2}]
    assert result["history"][0]["status"] == "failed"
    bus.retry(result["history"][0]["id"])
    bus.publish("test", "B", {"id": 2})
    bus.drain(max_events=1)
    assert bus.pending_count == 1
    bus.drain()
    assert received == [{"id": 2}, {"id": 1}, {"id": 2}]
    bus.reset()
    assert bus.snapshot() == {"pending": [], "history": []}


def test_queue_runs_concurrent_handlers_when_requested():
    bus = LocalEventBus()
    barrier = Barrier(2)
    bus.dispatch = lambda _event: barrier.wait(timeout=2)
    bus.publish("test", "A", {})
    bus.publish("test", "B", {})
    assert all(e["status"] == "completed" for e in bus.drain(workers=2)["history"])


def test_slack_records_wrapper_payloads_and_enforces_stream_lifecycle():
    slack = RecordingSlack()
    ts = slack.start_stream("C", "1", "T", "U", chunks=[{"type": "task_update"}])
    with pytest.raises(ValueError, match="mix"):
        slack.append_stream("C", ts, "wrong mode")
    slack.fail_next("chat.appendStream", after=True)
    with pytest.raises(RuntimeError, match="rate_limited"):
        slack.append_stream_chunks("C", ts, [{"type": "markdown_text", "text": "stored before failure"}])
    assert slack.streams[ts]["text"] == "stored before failure"
    slack.stop_stream("C", ts)
    with pytest.raises(RuntimeError, match="message_not_in_streaming_state"):
        slack.append_stream_chunks("C", ts, [{"type": "markdown_text", "text": "late"}])
    slack.reset()
    assert slack.start_stream("C", "1", "T", "U", "new") == "1000.000001"
