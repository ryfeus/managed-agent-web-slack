from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from managed_agents_app.managed_agent import ManagedAgentClient


def session(**overrides):
    value = {
        "id": "sesn_1",
        "title": "Title",
        "status": "idle",
        "created_at": "2026-09-03T00:00:00Z",
        "archived_at": None,
        "environment_id": "env_test",
        "metadata": {"creation_request_id": "request-1"},
    }
    value.update(overrides)
    return value


def raw_client() -> MagicMock:
    raw = MagicMock()
    raw.beta.sessions.events = MagicMock()
    return raw


def test_create_and_idempotency_lookup(config) -> None:
    raw = raw_client()
    raw.beta.sessions.create.return_value = session()
    raw.beta.sessions.list.return_value = [session(), session(id="sesn_2", metadata={})]
    managed = ManagedAgentClient(config, raw)

    created = managed.create_session(
        principal_id="principal-1",
        surface="slack",
        creation_request_id="request-1",
        initial_text="hello",
        system_context="context",
    )
    assert created.id == "sesn_1"
    kwargs = raw.beta.sessions.create.call_args.kwargs
    assert kwargs["metadata"]["owner_id"] == "principal-1"
    assert [event["type"] for event in kwargs["initial_events"]] == ["user.message"]
    assert kwargs["initial_events"][0]["content"][0]["text"].startswith("context\n\nUser request:")
    assert managed.find_by_creation_request_id("request-1").id == "sesn_1"


def test_message_tool_confirmation_interrupt_and_title(config) -> None:
    raw = raw_client()
    raw.beta.sessions.events.send.return_value = {"data": [{"id": "evt_1"}]}
    managed = ManagedAgentClient(config, raw)

    assert managed.send_message("sesn_1", "hello", "untrusted context") == "evt_1"
    managed.confirm_tool("sesn_1", "tool_1", False, "not approved")
    managed.interrupt("sesn_1")
    managed.update_title("sesn_1", "x" * 100)

    calls = raw.beta.sessions.events.send.call_args_list
    assert [event["type"] for event in calls[0].kwargs["events"]] == [
        "system.message",
        "user.message",
    ]
    assert calls[1].kwargs["events"][0] == {
        "type": "user.tool_confirmation",
        "tool_use_id": "tool_1",
        "result": "deny",
        "deny_message": "not approved",
    }
    assert calls[2].kwargs["events"] == [{"type": "user.interrupt"}]
    assert len(raw.beta.sessions.update.call_args.kwargs["title"]) == 60


def test_event_listing_and_streaming(config) -> None:
    raw = raw_client()
    raw.beta.sessions.events.list.return_value = [SimpleNamespace(model_dump=lambda **_: {"id": "evt_1"})]
    raw.beta.sessions.events.stream.return_value = iter([{"type": "agent.message", "id": "evt_2"}])
    managed = ManagedAgentClient(config, raw)
    assert managed.list_events("sesn_1") == [{"id": "evt_1"}]
    assert list(managed.stream_events("sesn_1", include_thinking=False)) == [
        {"type": "agent.message", "id": "evt_2"}
    ]
    assert raw.beta.sessions.events.stream.call_args.kwargs["event_deltas"] == ["agent.message"]
    assert raw.beta.sessions.events.stream.call_args.kwargs["timeout"] == 45.0


@pytest.mark.parametrize(
    ("field", "message"),
    [("agent_id", "Missing Claude agent ID"), ("environment_id", "Missing Claude environment ID")],
)
def test_missing_managed_agent_identifiers_fail_clearly(config, field, message) -> None:
    with pytest.raises(RuntimeError, match=message):
        ManagedAgentClient(config.model_copy(update={field: ""}), raw_client())
