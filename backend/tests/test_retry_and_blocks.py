from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from psycopg.errors import SerializationFailure
from slack_sdk.errors import SlackApiError

from managed_agents_app.db.connection import with_dsql_retry
from managed_agents_app.slack.blocks import (
    completed_task_chunk,
    denial_reason_modal,
    feedback_blocks,
    markdown_text_chunk,
    session_unfurl,
    source_link_blocks,
    tool_approval_blocks,
    working_task_chunk,
)
from managed_agents_app.slack.client import SlackClient


def test_serialization_retry_recovers(monkeypatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _: None)
    attempts = 0

    def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise SerializationFailure("retry")
        return "ok"

    assert with_dsql_retry(operation, attempts=3) == "ok"
    assert attempts == 3


def test_serialization_retry_exhausts(monkeypatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _: None)
    with pytest.raises(SerializationFailure):
        with_dsql_retry(lambda: (_ for _ in ()).throw(SerializationFailure("retry")), attempts=2)


def test_slack_blocks_contain_only_metadata_and_controls() -> None:
    approvals = tool_approval_blocks("tool_1", "browser", {"url": "https://example.test"})
    assert {item["action_id"] for item in approvals[1]["elements"]} == {
        "agent_tool_allow",
        "agent_tool_deny",
        "agent_tool_deny_with_reason",
    }
    assert feedback_blocks("evt_1")[0]["block_id"] == "feedback:evt_1"
    assert denial_reason_modal({"tool_use_id": "tool_1"})["callback_id"] == "agent_tool_deny_reason"
    unfurl = session_unfurl("sesn_1", "Title", "idle", "https://example.test/?session=sesn_1")
    assert unfurl["blocks"][1]["elements"][0]["action_id"] == "agent_link_session"
    assert working_task_chunk("Ev1", "https://example.test/message")["sources"][0]["text"] == (
        "Original message"
    )
    assert completed_task_chunk("Ev1")["status"] == "complete"
    assert markdown_text_chunk("hello") == {"type": "markdown_text", "text": "hello"}
    assert source_link_blocks("https://example.test/message")[0]["type"] == "context"
    assert source_link_blocks(None) == []


def test_slack_stream_adapter_uses_agent_apis() -> None:
    raw = MagicMock()
    raw.chat_startStream.return_value = {"ok": True, "ts": "10.2"}
    raw.chat_appendStream.return_value = {"ok": True}
    raw.chat_stopStream.return_value = {"ok": True}
    raw.agents_sessions_setStatus.return_value = {"ok": True}
    slack = SlackClient("xoxb-test", raw)

    assert slack.start_stream("C1", "10.1", "T1", "U1", "hello") == "10.2"
    slack.append_stream("C1", "10.2", " world")
    slack.stop_stream("C1", "10.2", status="active")
    slack.set_agent_status("C1", "10.1", "processing", initiator_user_id="U1")
    raw.chat_startStream.assert_called_once()
    raw.agents_sessions_setStatus.assert_called_once()


def test_slack_stream_adapter_enforces_and_uses_chunk_mode() -> None:
    raw = MagicMock()
    raw.chat_startStream.return_value = {"ok": True, "ts": "10.2"}
    raw.chat_appendStream.return_value = {"ok": True}
    raw.chat_stopStream.return_value = {"ok": True}
    slack = SlackClient("xoxb-test", raw)
    chunks = [working_task_chunk("Ev1")]

    assert slack.start_stream("C1", "10.1", "T1", "U1", chunks=chunks, task_display_mode="timeline") == "10.2"
    slack.append_stream_chunks("C1", "10.2", [markdown_text_chunk("hello")])
    slack.stop_stream("C1", "10.2", chunks=[completed_task_chunk("Ev1")])

    start = raw.chat_startStream.call_args.kwargs
    assert start["chunks"] == chunks
    assert start["task_display_mode"] == "timeline"
    assert "markdown_text" not in start
    assert raw.chat_appendStream.call_args.kwargs["chunks"][0]["type"] == "markdown_text"
    assert "markdown_text" not in raw.chat_stopStream.call_args.kwargs
    with pytest.raises(ValueError):
        slack.start_stream("C1", "10.1", "T1", "U1")
    with pytest.raises(ValueError):
        slack.start_stream("C1", "10.1", "T1", "U1", "text", chunks=chunks)
    with pytest.raises(ValueError):
        slack.stop_stream("C1", "10.2", text="text", chunks=chunks)


def test_slack_permalink_and_reaction_adapters() -> None:
    raw = MagicMock()
    raw.chat_getPermalink.return_value = {"ok": True, "permalink": "https://example.test/message"}
    raw.reactions_add.side_effect = [
        SlackApiError("duplicate", {"error": "already_reacted"}),
        SlackApiError("denied", {"error": "missing_scope"}),
    ]
    slack = SlackClient("xoxb-test", raw)

    assert slack.message_permalink("C1", "10.1") == "https://example.test/message"
    slack.add_reaction("C1", "10.1", "eyes")
    with pytest.raises(SlackApiError):
        slack.add_reaction("C1", "10.1", "eyes")

    raw.chat_getPermalink.return_value = {"ok": True}
    with pytest.raises(RuntimeError, match="no permalink"):
        slack.message_permalink("C1", "10.1")
