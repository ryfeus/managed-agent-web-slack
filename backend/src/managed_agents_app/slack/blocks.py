from __future__ import annotations

import json
from typing import Any


def markdown_text_chunk(text: str) -> dict[str, Any]:
    return {"type": "markdown_text", "text": text}


def working_task_chunk(request_id: str, permalink: str | None = None) -> dict[str, Any]:
    chunk: dict[str, Any] = {
        "type": "task_update",
        "id": f"request:{request_id}",
        "title": "Working on request",
        "status": "in_progress",
    }
    if permalink:
        chunk["sources"] = [{"type": "url", "text": "Original message", "url": permalink}]
    return chunk


def completed_task_chunk(request_id: str) -> dict[str, Any]:
    return {
        "type": "task_update",
        "id": f"request:{request_id}",
        "title": "Request complete",
        "status": "complete",
    }


def source_link_blocks(permalink: str | None) -> list[dict[str, Any]]:
    if not permalink:
        return []
    return [
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"<{permalink}|Original message>"}],
        }
    ]


def tool_approval_blocks(tool_use_id: str, name: str, input_value: object) -> list[dict[str, Any]]:
    rendered = json.dumps(input_value, indent=2, default=str)
    if len(rendered) > 2500:
        rendered = rendered[:2497] + "..."
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Agent wants to run a tool*\n*Tool:* `{name}`\n```{rendered}```",
            },
        },
        {
            "type": "actions",
            "block_id": f"tool:{tool_use_id}",
            "elements": [
                {
                    "type": "button",
                    "action_id": "agent_tool_allow",
                    "text": {"type": "plain_text", "text": "Allow"},
                    "style": "primary",
                    "value": tool_use_id,
                },
                {
                    "type": "button",
                    "action_id": "agent_tool_deny",
                    "text": {"type": "plain_text", "text": "Deny"},
                    "style": "danger",
                    "value": tool_use_id,
                },
                {
                    "type": "button",
                    "action_id": "agent_tool_deny_with_reason",
                    "text": {"type": "plain_text", "text": "Deny with reason"},
                    "value": tool_use_id,
                },
            ],
        },
    ]


def feedback_blocks(managed_event_id: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "context_actions",
            "block_id": f"feedback:{managed_event_id}",
            "elements": [
                {
                    "type": "feedback_buttons",
                    "action_id": "agent_feedback",
                    "positive_button": {
                        "text": {"type": "plain_text", "text": "Good response"},
                        "value": "positive",
                        "accessibility_label": "Mark this response as good",
                    },
                    "negative_button": {
                        "text": {"type": "plain_text", "text": "Bad response"},
                        "value": "negative",
                        "accessibility_label": "Mark this response as bad",
                    },
                }
            ],
        }
    ]


def denial_reason_modal(private_metadata: dict[str, str]) -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": "agent_tool_deny_reason",
        "private_metadata": json.dumps(private_metadata, separators=(",", ":")),
        "title": {"type": "plain_text", "text": "Deny tool"},
        "submit": {"type": "plain_text", "text": "Deny"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "reason",
                "label": {"type": "plain_text", "text": "Reason"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "value",
                    "multiline": True,
                    "max_length": 1000,
                },
            }
        ],
    }


def session_unfurl(session_id: str, title: str, status: str, url: str) -> dict[str, Any]:
    return {
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Claude Session*\n{title or 'Untitled session'}\nStatus: {status}",
                },
            },
            {
                "type": "actions",
                "block_id": f"session:{session_id}",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "agent_link_session",
                        "text": {"type": "plain_text", "text": "Link this thread"},
                        "value": session_id,
                    },
                    {
                        "type": "button",
                        "action_id": "agent_open_session",
                        "text": {"type": "plain_text", "text": "Open in web"},
                        "url": url,
                    },
                ],
            },
        ]
    }
