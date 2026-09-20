from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from managed_agents_app.domain import (
    SLACK_FEEDBACK_RECEIVED,
    SLACK_MESSAGE_RECEIVED,
    SLACK_SESSION_LINK_REQUESTED,
    SLACK_SESSION_LINK_SHARED,
    SLACK_SESSION_STOP_REQUESTED,
    SLACK_SHORTCUT_RECEIVED,
    SLACK_TOOL_CONFIRMATION_REQUESTED,
    SlackFeedbackReceived,
    SlackMessageReceived,
    SlackSessionLinkRequested,
    SlackSessionLinkShared,
    SlackSessionStopRequested,
    SlackShortcutReceived,
    SlackToolConfirmationRequested,
)
from managed_agents_app.slack.signatures import interaction_id

SESSION_RE = re.compile(r"^link\s+(sesn_[A-Za-z0-9_-]+)$", re.IGNORECASE)


@dataclass(frozen=True)
class ModalOpenRequest:
    trigger_id: str
    metadata: dict[str, str]


def normalize_event(envelope: dict[str, Any]) -> tuple[str, Any] | None:
    if (
        envelope.get("type") != "event_callback"
        or not envelope.get("event_id")
        or not envelope.get("team_id")
    ):
        return None
    event = envelope.get("event") or {}
    event_type = event.get("type")
    if event.get("bot_id") or event.get("subtype"):
        return None

    if event_type in {"app_mention", "message"}:
        if not all(isinstance(event.get(key), str) for key in ("user", "channel", "ts")):
            return None
        channel_type = event.get("channel_type")
        if event_type == "message" and channel_type != "im" and not event.get("thread_ts"):
            return None
        text = re.sub(r"<@[A-Z0-9]+>", "", str(event.get("text") or ""), flags=re.IGNORECASE).strip()
        link = SESSION_RE.match(text)
        context = event.get("context") or envelope.get("context")
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
                link_session_id=link.group(1) if link else None,
                channel_type=channel_type,
                active_context=context if isinstance(context, dict) else None,
                event_type=event_type,
            ),
        )

    if event_type == "agent_session_stopped" and all(
        isinstance(event.get(key), str) for key in ("user", "channel", "thread_ts")
    ):
        return (
            SLACK_SESSION_STOP_REQUESTED,
            SlackSessionStopRequested(
                event_id=envelope["event_id"],
                team_id=envelope["team_id"],
                channel_id=event["channel"],
                thread_ts=event["thread_ts"],
                user_id=event["user"],
            ),
        )

    if event_type == "link_shared" and all(
        isinstance(event.get(key), str) for key in ("user", "channel", "message_ts")
    ):
        urls = [item["url"] for item in event.get("links", []) if isinstance(item.get("url"), str)]
        if not urls:
            return None
        return (
            SLACK_SESSION_LINK_SHARED,
            SlackSessionLinkShared(
                event_id=envelope["event_id"],
                team_id=envelope["team_id"],
                channel_id=event["channel"],
                message_ts=event["message_ts"],
                user_id=event["user"],
                urls=urls,
            ),
        )

    return None


def normalize_interaction(
    payload: dict[str, Any], raw_body: str
) -> tuple[str, Any] | ModalOpenRequest | None:
    identity = interaction_id(raw_body)
    team_id = _nested(payload, "team", "id")
    user_id = _nested(payload, "user", "id")
    channel_id = _nested(payload, "channel", "id") or _nested(payload, "container", "channel_id")
    message_ts = _nested(payload, "message", "ts") or _nested(payload, "container", "message_ts")
    thread_ts = _nested(payload, "message", "thread_ts") or message_ts

    if payload.get("type") in {"message_action", "shortcut"}:
        callback_id = payload.get("callback_id")
        if (
            callback_id in {"ask_agent", "summarize_thread", "investigate"}
            and team_id
            and user_id
            and channel_id
            and message_ts
        ):
            return (
                SLACK_SHORTCUT_RECEIVED,
                SlackShortcutReceived(
                    interaction_id=identity,
                    team_id=team_id,
                    channel_id=channel_id,
                    message_ts=message_ts,
                    thread_ts=thread_ts,
                    user_id=user_id,
                    callback_id=callback_id,
                ),
            )
        return None

    if (
        payload.get("type") == "view_submission"
        and _nested(payload, "view", "callback_id") == "agent_tool_deny_reason"
    ):
        try:
            metadata = json.loads(_nested(payload, "view", "private_metadata") or "{}")
            reason = payload["view"]["state"]["values"]["reason"]["value"].get("value")
            return (
                SLACK_TOOL_CONFIRMATION_REQUESTED,
                SlackToolConfirmationRequested(
                    interaction_id=identity,
                    team_id=metadata["team_id"],
                    channel_id=metadata["channel_id"],
                    thread_ts=metadata["thread_ts"],
                    user_id=user_id,
                    tool_use_id=metadata["tool_use_id"],
                    approved=False,
                    reason=reason,
                    response_message_ts=metadata.get("response_message_ts"),
                ),
            )
        except KeyError, TypeError, ValueError:
            return None

    if payload.get("type") != "block_actions" or not payload.get("actions"):
        return None
    action = payload["actions"][0]
    action_id = action.get("action_id")
    value = action.get("value")
    if not all((team_id, user_id, channel_id, thread_ts, action_id, value)):
        return None

    if action_id == "agent_tool_deny_with_reason":
        trigger_id = payload.get("trigger_id")
        if not trigger_id:
            return None
        return ModalOpenRequest(
            trigger_id=trigger_id,
            metadata={
                "team_id": team_id,
                "channel_id": channel_id,
                "thread_ts": thread_ts,
                "tool_use_id": value,
                "response_message_ts": message_ts or "",
            },
        )
    if action_id in {"agent_tool_allow", "agent_tool_deny"}:
        return (
            SLACK_TOOL_CONFIRMATION_REQUESTED,
            SlackToolConfirmationRequested(
                interaction_id=identity,
                team_id=team_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                user_id=user_id,
                tool_use_id=value,
                approved=action_id == "agent_tool_allow",
                response_message_ts=message_ts,
            ),
        )
    if action_id == "agent_feedback" and value in {"positive", "negative"}:
        block_id = action.get("block_id") or payload.get("actions", [{}])[0].get("block_id")
        if not block_id:
            block_id = next(
                (block.get("block_id") for block in payload.get("message", {}).get("blocks", [])), ""
            )
        managed_event_id = block_id.removeprefix("feedback:") if block_id.startswith("feedback:") else None
        return (
            SLACK_FEEDBACK_RECEIVED,
            SlackFeedbackReceived(
                interaction_id=identity,
                team_id=team_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                user_id=user_id,
                rating=value,
                managed_event_id=managed_event_id,
                external_message_id=message_ts,
            ),
        )
    if action_id == "agent_link_session":
        return (
            SLACK_SESSION_LINK_REQUESTED,
            SlackSessionLinkRequested(
                interaction_id=identity,
                team_id=team_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                user_id=user_id,
                session_id=value,
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
