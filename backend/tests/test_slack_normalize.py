from __future__ import annotations

import json

from managed_agents_app.domain import (
    SLACK_FEEDBACK_RECEIVED,
    SLACK_MESSAGE_RECEIVED,
    SLACK_SHORTCUT_RECEIVED,
    SLACK_THREAD_LINK_REQUESTED,
    SLACK_THREAD_LINK_SHARED,
    SLACK_THREAD_STOP_REQUESTED,
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
    assert stop and stop[0] == SLACK_THREAD_STOP_REQUESTED

    shared = normalize_event(
        envelope(
            {
                "type": "link_shared",
                "user": "U1",
                "channel": "C1",
                "message_ts": "3",
                "links": [{"url": "https://example.test/?thread=00000000-0000-4000-8000-000000000001"}],
            }
        )
    )
    assert shared and shared[0] == SLACK_THREAD_LINK_SHARED

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


def test_tool_feedback_and_link_interactions() -> None:
    raw = "payload=one"
    approval_value = json.dumps({"taskId": "task-1", "requestId": "req-1"})
    allow = normalize_interaction(block_payload("agent_tool_allow", approval_value), raw)
    assert allow and not isinstance(allow, ModalOpenRequest)
    assert allow[0] == SLACK_TOOL_CONFIRMATION_REQUESTED and allow[1].approved
    assert allow[1].interaction_id == interaction_id(raw)

    modal = block_payload("agent_tool_deny_with_reason", approval_value)
    modal["trigger_id"] = "trigger"
    result = normalize_interaction(modal, raw)
    assert isinstance(result, ModalOpenRequest)

    feedback = block_payload("agent_feedback", "positive")
    feedback["actions"][0]["block_id"] = "feedback:task-1:message-1"
    result = normalize_interaction(feedback, raw)
    assert result and not isinstance(result, ModalOpenRequest)
    assert result[0] == SLACK_FEEDBACK_RECEIVED and result[1].message_id == "message-1"

    linked = normalize_interaction(block_payload("agent_link_thread", "thread-1"), raw)
    assert linked and not isinstance(linked, ModalOpenRequest)
    assert linked[0] == SLACK_THREAD_LINK_REQUESTED


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
            "task_id": "task-1",
            "request_id": "req-1",
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
