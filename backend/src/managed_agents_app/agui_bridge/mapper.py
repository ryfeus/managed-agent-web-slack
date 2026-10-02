"""Provider-neutral A2A Task to AG-UI event and history mapping."""

from __future__ import annotations

import json
from typing import Any

from a2a.types import a2a_pb2 as a2a
from ag_ui.core import (
    AssistantMessage,
    Interrupt,
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunFinishedSuccessOutcome,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    UserMessage,
)
from ag_ui.core import Message as AgUiMessage

from managed_agents_app.protocols.human_input import ClarificationRequest, ToolApprovalRequest
from managed_agents_app.protocols.human_input_wire import requests_from_task

FINAL_STATES = {
    a2a.TaskState.TASK_STATE_COMPLETED,
    a2a.TaskState.TASK_STATE_CANCELED,
    a2a.TaskState.TASK_STATE_FAILED,
    a2a.TaskState.TASK_STATE_REJECTED,
    a2a.TaskState.TASK_STATE_INPUT_REQUIRED,
}
ACTIVE_STATES = {
    a2a.TaskState.TASK_STATE_SUBMITTED,
    a2a.TaskState.TASK_STATE_WORKING,
    a2a.TaskState.TASK_STATE_INPUT_REQUIRED,
}


def message_text(message: a2a.Message) -> str:
    return "\n".join(part.text for part in message.parts if part.WhichOneof("content") == "text")


def interrupts_for(task: a2a.Task) -> list[Interrupt]:
    result: list[Interrupt] = []
    for request in requests_from_task(task):
        if isinstance(request, ToolApprovalRequest):
            result.append(
                Interrupt(
                    id=request.request_id,
                    reason="tool_approval",
                    message=request.message,
                    tool_call_id=request.request_id,
                    metadata=request.model_dump(by_alias=True),
                )
            )
        elif isinstance(request, ClarificationRequest):
            result.append(
                Interrupt(
                    id=request.request_id,
                    reason="clarification",
                    message=request.prompt,
                    metadata=request.model_dump(by_alias=True),
                )
            )
    return result


def history_message(message: a2a.Message) -> AgUiMessage | None:
    content = message_text(message)
    if message.role == a2a.Role.ROLE_USER:
        return UserMessage(id=message.message_id, content=content)
    if message.role == a2a.Role.ROLE_AGENT:
        return AssistantMessage(id=message.message_id, content=content)
    return None


def pending_history_message(task: a2a.Task) -> AssistantMessage | None:
    interrupts = interrupts_for(task)
    if not interrupts:
        return None
    tools = [
        {
            "id": item.id,
            "type": "function",
            "function": {
                "name": item.metadata["tool"]["name"],
                "arguments": json.dumps(item.metadata["tool"]["arguments"]),
            },
        }
        for item in interrupts
        if item.reason == "tool_approval" and item.metadata
    ]
    return AssistantMessage(
        id=f"pending-{task.id}",
        content="",
        tool_calls=tools or None,
        metadata={
            "custom": {
                "agui": {
                    "interrupts": [item.model_dump(by_alias=True, exclude_none=True) for item in interrupts]
                }
            }
        },
    )


def agent_message_events(message: a2a.Message) -> list[Any]:
    text = message_text(message)
    if not text:
        return []
    return [
        TextMessageStartEvent(message_id=message.message_id, role="assistant"),
        TextMessageContentEvent(message_id=message.message_id, delta=text),
        TextMessageEndEvent(message_id=message.message_id),
    ]


def finish_events(task: a2a.Task, thread_id: str, run_id: str) -> list[Any]:
    state = task.status.state
    if state == a2a.TaskState.TASK_STATE_INPUT_REQUIRED:
        interrupts = interrupts_for(task)
        if not interrupts:
            return [RunErrorEvent(message="This task requires unsupported input.")]
        events: list[Any] = []
        for item in interrupts:
            if item.reason == "tool_approval" and item.metadata:
                tool = item.metadata["tool"]
                events.extend(
                    [
                        ToolCallStartEvent(tool_call_id=item.id, tool_call_name=tool["name"]),
                        ToolCallArgsEvent(tool_call_id=item.id, delta=json.dumps(tool["arguments"])),
                        ToolCallEndEvent(tool_call_id=item.id),
                    ]
                )
        events.append(
            RunFinishedEvent(
                thread_id=thread_id,
                run_id=run_id,
                outcome=RunFinishedInterruptOutcome(interrupts=interrupts),
            )
        )
        return events
    if state == a2a.TaskState.TASK_STATE_COMPLETED:
        return [RunFinishedEvent(thread_id=thread_id, run_id=run_id, outcome=RunFinishedSuccessOutcome())]
    if state == a2a.TaskState.TASK_STATE_CANCELED:
        # The pinned assistant-ui AG-UI adapter accepts success and interrupt
        # outcomes but drops AG-UI 1.0's cancelled outcome. A safe error keeps
        # remote cancellation visible instead of misreporting success.
        return [RunErrorEvent(message="The task was cancelled.")]
    if state in {a2a.TaskState.TASK_STATE_FAILED, a2a.TaskState.TASK_STATE_REJECTED}:
        safe = message_text(task.status.message) if task.status.HasField("message") else ""
        return [RunErrorEvent(message=safe or "The task could not be completed.")]
    return []
