from __future__ import annotations

import json

from managed_agents_app.domain import (
    SLACK_MESSAGE_RECEIVED,
    SLACK_THREAD_STOP_REQUESTED,
    SLACK_TOOL_CONFIRMATION_REQUESTED,
)
from managed_agents_app.slack.normalize import normalize_event, normalize_interaction
from managed_agents_app.slack.signatures import interaction_id


def envelope(event: dict) -> dict:
    return {"type": "event_callback", "event_id": "Ev1", "team_id": "T1", "event": event}


def test_mention_and_dm_normalization() -> None:
    mention = normalize_event(
        envelope(
            {
                "type": "app_mention",
                "user": "U1",
                "channel": "C1",
                "ts": "10.1",
                "text": "<@UBOT> investigate this",
            }
        )
    )
    assert mention and mention[0] == SLACK_MESSAGE_RECEIVED
    assert mention[1].text == "investigate this"
    assert mention[1].thread_ts == "10.1"

    dm = normalize_event(
        envelope(
            {
                "type": "message",
                "channel_type": "im",
                "user": "U1",
                "channel": "D1",
                "ts": "11.1",
                "text": "hello",
                "context": {
                    "entities": [
                        {
                            "type": "slack#/types/message_context",
                            "value": {"channel_id": "C2", "message_ts": "9.1"},
                            "team_id": "T1",
                        }
                    ]
                },
            }
        )
    )
    assert dm and dm[1].thread_ts == "11.1"


def test_channel_message_rules_and_bot_filtering() -> None:
    assert (
        normalize_event(
            envelope(
                {
                    "type": "message",
                    "user": "U1",
                    "channel": "C1",
                    "ts": "1",
                    "text": "top-level noise",
                }
            )
        )
        is None
    )
    reply = normalize_event(
        envelope(
            {
                "type": "message",
                "user": "U1",
                "channel": "C1",
                "thread_ts": "1",
                "ts": "2",
                "text": "natural reply",
            }
        )
    )
    assert reply and reply[1].thread_ts == "1"
    assert normalize_event(envelope({"type": "message", "bot_id": "B1"})) is None
    assert normalize_event(envelope({"type": "message", "subtype": "message_changed"})) is None


def test_only_configured_user_token_bot_and_app_are_accepted() -> None:
    mention = envelope(
        {
            "type": "app_mention",
            "user": "U1",
            "channel": "C1",
            "ts": "1",
            "text": "<@UBOT> hello",
            "bot_id": "B_USER",
            "app_id": "A_USER",
        }
    )
    assert normalize_event(mention) is None
    assert normalize_event(mention, allowed_bot_id="B_USER") is None
    assert normalize_event(mention, allowed_app_id="A_USER") is None
    assert normalize_event(mention, allowed_bot_id="B_OTHER", allowed_app_id="A_USER") is None
    assert normalize_event(mention, allowed_bot_id="B_USER", allowed_app_id="A_OTHER") is None
    accepted = normalize_event(mention, allowed_bot_id="B_USER", allowed_app_id="A_USER")
    assert accepted and accepted[0] == SLACK_MESSAGE_RECEIVED
    assert accepted[1].user_id == "U1"

    reply = envelope(
        {
            **mention["event"],
            "type": "message",
            "ts": "2",
            "thread_ts": "1",
            "text": "continue",
        }
    )
    accepted_reply = normalize_event(reply, allowed_bot_id="B_USER", allowed_app_id="A_USER")
    assert accepted_reply and accepted_reply[1].thread_ts == "1"
    reply["event"].pop("thread_ts")
    assert normalize_event(reply, allowed_bot_id="B_USER", allowed_app_id="A_USER") is None
    reply["event"].update(thread_ts="1", subtype="bot_message")
    assert normalize_event(reply, allowed_bot_id="B_USER", allowed_app_id="A_USER") is not None
    reply["event"]["subtype"] = "message_changed"
    assert normalize_event(reply, allowed_bot_id="B_USER", allowed_app_id="A_USER") is None


def test_stop_and_explicit_link() -> None:
    stop = normalize_event(
        envelope(
            {
                "type": "agent_session_stopped",
                "user": "U1",
                "channel": "D1",
                "thread_ts": "1",
            }
        )
    )
    assert stop and stop[0] == SLACK_THREAD_STOP_REQUESTED

    link = normalize_event(
        envelope(
            {
                "type": "app_mention",
                "user": "U1",
                "channel": "C1",
                "ts": "4",
                "text": "<@UBOT> link 00000000-0000-4000-8000-000000000001",
            }
        )
    )
    assert link and link[1].command == "link"
    assert link[1].link_thread_id == "00000000-0000-4000-8000-000000000001"


def block_payload(action_id: str, value: str) -> dict:
    return {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": "U1"},
        "channel": {"id": "C1"},
        "message": {"ts": "2", "thread_ts": "1"},
        "actions": [{"action_id": action_id, "value": value}],
    }


def test_tool_allow_and_deny_interactions():
    raw = "payload=one"
    value = json.dumps({"taskId": "task-1", "requestId": "req-1"})
    for action, approved in (("agent_tool_allow", True), ("agent_tool_deny", False)):
        result = normalize_interaction(block_payload(action, value), raw)
        assert result and result[0] == SLACK_TOOL_CONFIRMATION_REQUESTED
        assert result[1].approved == approved and result[1].interaction_id == interaction_id(raw)
        assert result[1].team_id == "T1" and result[1].user_id == "U1"
