from __future__ import annotations

from typing import Any


def pending_tool_ids(events: list[dict[str, Any]]) -> set[str]:
    """Return the tool IDs requested by the most recent idle event."""
    for event in reversed(events):
        if event.get("type") not in {"session.status_idle", "session.thread_status_idle"}:
            continue
        stop_reason = event.get("stop_reason") or {}
        if stop_reason.get("type") != "requires_action":
            return set()
        return {str(value) for value in stop_reason.get("event_ids", [])}
    return set()


def tool_is_pending(events: list[dict[str, Any]], tool_use_id: str) -> bool:
    return tool_use_id in pending_tool_ids(events)
