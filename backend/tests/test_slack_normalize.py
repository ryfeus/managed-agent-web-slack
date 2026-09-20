from __future__ import annotations

import json

from managed_agents_app.domain import (
    SLACK_FEEDBACK_RECEIVED,
    SLACK_MESSAGE_RECEIVED,
    SLACK_SESSION_LINK_REQUESTED,
    SLACK_SESSION_LINK_SHARED,
    SLACK_SESSION_STOP_REQUESTED,
    SLACK_SHORTCUT_RECEIVED,
    SLACK_TOOL_CONFIRMATION_REQUESTED,
)
from managed_agents_app.slack.normalize import ModalOpenRequest, normalize_event, normalize_interaction
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
    assert dm[1].active_context["entities"][0]["value"]["channel_id"] == "C2"


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


def test_stop_link_shared_and_compatibility_link() -> None:
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
    assert stop and stop[0] == SLACK_SESSION_STOP_REQUESTED

    shared = normalize_event(
        envelope(
            {
                "type": "link_shared",
                "user": "U1",
                "channel": "C1",
                "message_ts": "3",
                "links": [{"url": "https://example.test/?session=sesn_1"}],
            }
        )
    )
    assert shared and shared[0] == SLACK_SESSION_LINK_SHARED

    link = normalize_event(
        envelope(
            {
                "type": "app_mention",
                "user": "U1",
                "channel": "C1",
                "ts": "4",
                "text": "<@UBOT> link sesn_abc-123",
            }
        )
    )
    assert link and link[1].command == "link" and link[1].link_session_id == "sesn_abc-123"


def block_payload(action_id: str, value: str) -> dict:
    return {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": "U1"},
        "channel": {"id": "C1"},
        "message": {"ts": "2", "thread_ts": "1"},
        "actions": [{"action_id": action_id, "value": value}],
    }


def test_tool_feedback_and_link_interactions() -> None:
    raw = "payload=one"
    allow = normalize_interaction(block_payload("agent_tool_allow", "tool_1"), raw)
    assert allow and not isinstance(allow, ModalOpenRequest)
    assert allow[0] == SLACK_TOOL_CONFIRMATION_REQUESTED and allow[1].approved
    assert allow[1].interaction_id == interaction_id(raw)

    modal = block_payload("agent_tool_deny_with_reason", "tool_1")
    modal["trigger_id"] = "trigger"
    result = normalize_interaction(modal, raw)
    assert isinstance(result, ModalOpenRequest)

    feedback = block_payload("agent_feedback", "positive")
    feedback["actions"][0]["block_id"] = "feedback:evt_1"
    result = normalize_interaction(feedback, raw)
    assert result and not isinstance(result, ModalOpenRequest)
    assert result[0] == SLACK_FEEDBACK_RECEIVED and result[1].managed_event_id == "evt_1"

    linked = normalize_interaction(block_payload("agent_link_session", "sesn_1"), raw)
    assert linked and not isinstance(linked, ModalOpenRequest)
    assert linked[0] == SLACK_SESSION_LINK_REQUESTED


def test_shortcut_and_denial_reason_submission() -> None:
    raw = "payload=shortcut"
    shortcut = normalize_interaction(
        {
            "type": "message_action",
            "callback_id": "summarize_thread",
            "team": {"id": "T1"},
            "user": {"id": "U1"},
            "channel": {"id": "C1"},
            "message": {"ts": "2", "thread_ts": "1"},
        },
        raw,
    )
    assert shortcut and not isinstance(shortcut, ModalOpenRequest)
    assert shortcut[0] == SLACK_SHORTCUT_RECEIVED

    metadata = json.dumps(
        {
            "team_id": "T1",
            "channel_id": "C1",
            "thread_ts": "1",
            "tool_use_id": "tool_1",
        }
    )
    denial = normalize_interaction(
        {
            "type": "view_submission",
            "user": {"id": "U1"},
            "view": {
                "callback_id": "agent_tool_deny_reason",
                "private_metadata": metadata,
                "state": {"values": {"reason": {"value": {"value": "unsafe"}}}},
            },
        },
        raw,
    )
    assert denial and not isinstance(denial, ModalOpenRequest)
    assert denial[0] == SLACK_TOOL_CONFIRMATION_REQUESTED
    assert denial[1].reason == "unsafe" and not denial[1].approved
