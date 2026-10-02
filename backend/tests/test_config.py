from __future__ import annotations

import pytest
from pydantic import ValidationError

from managed_agents_app.config import _secret, load_config


@pytest.fixture
def clean_config(monkeypatch):
    monkeypatch.setenv("APP_ENV", "e2e")
    monkeypatch.delenv("CLAUDE_AGENT_VERSION", raising=False)
    load_config.cache_clear()
    _secret.cache_clear()
    yield
    load_config.cache_clear()
    _secret.cache_clear()


def test_load_exact_agent_version(clean_config, monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT_VERSION", "7")
    assert load_config().agent_version == 7


def test_surface_configuration_does_not_require_provider_pin(clean_config):
    assert load_config().agent_version is None


@pytest.mark.parametrize("value", ["", "0", "-1", "1.5", "latest", "true", " 7", "７"])
def test_invalid_environment_version_is_clear(clean_config, monkeypatch, value):
    monkeypatch.setenv("CLAUDE_AGENT_VERSION", value)
    with pytest.raises(RuntimeError, match="CLAUDE_AGENT_VERSION must be a positive integer"):
        load_config()


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "7"])
def test_config_rejects_invalid_version(config, value):
    with pytest.raises(ValidationError):
        type(config)(**(config.model_dump() | {"agent_version": value}))


def test_legacy_agent_alias_does_not_override_lock_input(clean_config, monkeypatch):
    monkeypatch.setenv("AGENT_ID", "agent_stale")
    monkeypatch.delenv("CLAUDE_AGENT_ID", raising=False)
    assert load_config().agent_id == ""
