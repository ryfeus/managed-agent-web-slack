"""Canonical browser history derived only from A2A Tasks."""

from __future__ import annotations

from typing import Any

from a2a.types import a2a_pb2 as a2a
from ag_ui.core import Message as AgUiMessage

from managed_agents_app.agent_control_plane.service import ThreadAgentService
from managed_agents_app.agui_bridge.mapper import (
    history_message,
    interrupts_for,
    pending_history_message,
)


async def canonical_history(service: ThreadAgentService, thread_id: str) -> dict[str, Any]:
    listed = await service.list_tasks(thread_id)
    messages: list[AgUiMessage] = []
    seen: set[str] = set()
    for item in listed:
        task = await service.get_task(thread_id, item.id, human_input=True)
        for original in task.history:
            if original.message_id in seen:
                continue
            converted = history_message(original)
            if converted is not None:
                seen.add(original.message_id)
                messages.append(converted)
    active = await service.get_active_task(thread_id, human_input=True)
    if active is not None:
        pending = pending_history_message(active)
        if pending is not None:
            messages.append(pending)
    return {
        "threadId": thread_id,
        "messages": [message.model_dump(by_alias=True, exclude_none=True) for message in messages],
        "activeTaskId": active.id if active else None,
        "activeTaskState": a2a.TaskState.Name(active.status.state) if active else None,
        "hasPendingInterrupts": bool(active and interrupts_for(active)),
    }
