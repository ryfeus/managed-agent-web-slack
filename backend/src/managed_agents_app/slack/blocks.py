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
    task_id: str, request_id: str, name: str, input_value: object, explanation: str | None = None
) -> list[dict[str, Any]]:
    rendered = json.dumps(input_value, indent=2, default=str)
    value = json.dumps({"taskId": task_id, "requestId": request_id}, separators=(",", ":"))
    if len(rendered) > 2500:
        rendered = rendered[:2497] + "..."
    return response_text_blocks(explanation or "OpenAPPA requests review of this exact call.") + [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "verbatim": True,
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
            ],
        },
    ]
