from __future__ import annotations

import json
from typing import Any


def markdown_text_chunk(text: str) -> dict[str, Any]:
    return {"type": "markdown_text", "text": text}


def response_text_blocks(text: str) -> list[dict[str, Any]]:
    """Keep the answer visible when postMessage also includes controls or context."""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text[start : start + 3000]}}
        for start in range(0, len(text), 3000)
    ]


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


def tool_approval_blocks(
    task_id: str, request_id: str, name: str, input_value: object
) -> list[dict[str, Any]]:
    rendered = json.dumps(input_value, indent=2, default=str)
    value = json.dumps({"taskId": task_id, "requestId": request_id}, separators=(",", ":"))
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
            "block_id": f"tool:{request_id}",
            "elements": [
                {
                    "type": "button",
                    "action_id": "agent_tool_allow",
                    "text": {"type": "plain_text", "text": "Allow"},
                    "style": "primary",
                    "value": value,
                },
                {
                    "type": "button",
                    "action_id": "agent_tool_deny",
                    "text": {"type": "plain_text", "text": "Deny"},
                    "style": "danger",
                    "value": value,
                },
                {
                    "type": "button",
                    "action_id": "agent_tool_deny_with_reason",
                    "text": {"type": "plain_text", "text": "Deny with reason"},
                    "value": value,
                },
            ],
        },
    ]


def feedback_blocks(task_id: str, message_id: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "context_actions",
            "block_id": f"feedback:{task_id}:{message_id}",
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


def thread_unfurl(thread_id: str, title: str, status: str, url: str) -> dict[str, Any]:
    return {
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Claude Thread*\n{title or 'Untitled thread'}\nStatus: {status}",
                },
            },
            {
                "type": "actions",
                "block_id": f"thread:{thread_id}",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "agent_link_thread",
                        "text": {"type": "plain_text", "text": "Link this thread"},
                        "value": thread_id,
                    },
                    {
                        "type": "button",
                        "action_id": "agent_open_thread",
                        "text": {"type": "plain_text", "text": "Open in web"},
                        "url": url,
                    },
                ],
            },
        ]
    }
