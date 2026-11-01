from __future__ import annotations

import json
import re
from typing import Any

from managed_agents_app.domain import (
    SLACK_MESSAGE_RECEIVED,
    SLACK_THREAD_STOP_REQUESTED,
    SLACK_TOOL_CONFIRMATION_REQUESTED,
    SlackMessageReceived,
    SlackThreadStopRequested,
    SlackToolConfirmationRequested,
)
from managed_agents_app.slack.signatures import interaction_id

THREAD_RE = re.compile(r"^link\s+([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$", re.I)


def normalize_event(
    envelope: dict[str, Any], *, allowed_bot_id: str | None = None, allowed_app_id: str | None = None
) -> tuple[str, Any] | None:
    if (
        envelope.get("type") != "event_callback"
        or not envelope.get("event_id")
        or not envelope.get("team_id")
    ):
        return None
    event = envelope.get("event") or {}
    event_type = event.get("type")
    allowed_app_bot = bool(
        event_type in {"app_mention", "message"}
        and allowed_bot_id
        and allowed_app_id
        and event.get("bot_id") == allowed_bot_id
        and event.get("app_id") == allowed_app_id
    )
    if event.get("subtype") and not (event.get("subtype") == "bot_message" and allowed_app_bot):
        return None
    if event.get("bot_id") and not allowed_app_bot:
        return None

    if event_type in {"app_mention", "message"}:
        if not all(isinstance(event.get(key), str) for key in ("user", "channel", "ts")):
            return None
        channel_type = event.get("channel_type")
        if event_type == "message" and channel_type != "im" and not event.get("thread_ts"):
            return None
        text = re.sub(r"<@[A-Z0-9]+>", "", str(event.get("text") or ""), flags=re.IGNORECASE).strip()
        link = THREAD_RE.match(text)
        return (
            SLACK_MESSAGE_RECEIVED,
            SlackMessageReceived(
                slack_event_id=envelope["event_id"],
                team_id=envelope["team_id"],
                channel_id=event["channel"],
                thread_ts=event.get("thread_ts") or event["ts"],
                message_ts=event["ts"],
                user_id=event["user"],
                text=text,
                command="link" if link else "message",
                link_thread_id=link.group(1) if link else None,
                channel_type=channel_type,
                event_type=event_type,
            ),
        )

    if event_type == "agent_session_stopped" and all(
        isinstance(event.get(key), str) for key in ("user", "channel", "thread_ts")
    ):
        return (
            SLACK_THREAD_STOP_REQUESTED,
            SlackThreadStopRequested(
                event_id=envelope["event_id"],
                team_id=envelope["team_id"],
                channel_id=event["channel"],
                thread_ts=event["thread_ts"],
                user_id=event["user"],
            ),
        )

    return None


def normalize_interaction(payload: dict[str, Any], raw_body: str) -> tuple[str, Any] | None:
    identity = interaction_id(raw_body)
    team_id = _nested(payload, "team", "id")
    user_id = _nested(payload, "user", "id")
    channel_id = _nested(payload, "channel", "id") or _nested(payload, "container", "channel_id")
    message_ts = _nested(payload, "message", "ts") or _nested(payload, "container", "message_ts")
    thread_ts = _nested(payload, "message", "thread_ts") or message_ts

    if payload.get("type") != "block_actions" or not payload.get("actions"):
        return None
    action = payload["actions"][0]
    action_id = action.get("action_id")
    value = action.get("value")
    if not all((team_id, user_id, channel_id, thread_ts, action_id, value)):
        return None

    if action_id in {"agent_tool_allow", "agent_tool_deny"}:
        try:
            identifiers = json.loads(value)
            task_id, request_id = identifiers["taskId"], identifiers["requestId"]
        except ValueError, TypeError, KeyError:
            return None
        return (
            SLACK_TOOL_CONFIRMATION_REQUESTED,
            SlackToolConfirmationRequested(
                interaction_id=identity,
                team_id=team_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                user_id=user_id,
                task_id=task_id,
                input_request_id=request_id,
                approved=action_id == "agent_tool_allow",
                response_message_ts=message_ts,
            ),
        )
    return None


def _nested(value: dict[str, Any], *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current
