"""Build A2A task views from provider history and controller metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from a2a.types import a2a_pb2 as a2a

from managed_agents_app.protocols.controller_profile import HUMAN_INPUT_EXTENSION_URI
from managed_agents_app.protocols.human_input import ClarificationRequest, ToolApprovalRequest, ToolDescriptor

_STATES = {
    "SUBMITTED": a2a.TaskState.TASK_STATE_SUBMITTED,
    "WORKING": a2a.TaskState.TASK_STATE_WORKING,
    "COMPLETED": a2a.TaskState.TASK_STATE_COMPLETED,
    "FAILED": a2a.TaskState.TASK_STATE_FAILED,
    "CANCELED": a2a.TaskState.TASK_STATE_CANCELED,
    "INPUT_REQUIRED": a2a.TaskState.TASK_STATE_INPUT_REQUIRED,
    "REJECTED": a2a.TaskState.TASK_STATE_REJECTED,
}


@dataclass(frozen=True)
class TaskViewOptions:
    history_length: int | None = None
    enabled_extensions: frozenset[str] = frozenset()

    @property
    def human_input_enabled(self) -> bool:
        return HUMAN_INPUT_EXTENSION_URI in self.enabled_extensions


def text_from_event(event: dict[str, Any]) -> str:
    return "".join(
        str(part.get("text", "")) for part in event.get("content", []) if part.get("type") == "text"
    )


def _message(task: dict[str, Any], event: dict[str, Any]) -> a2a.Message:
    role = a2a.Role.ROLE_USER if event["type"] == "user.message" else a2a.Role.ROLE_AGENT
    message_id = str(task["message_id"]) if role == a2a.Role.ROLE_USER else f"cma-{event['id']}"
    return a2a.Message(
        message_id=message_id,
        context_id=str(task["context_id"]),
        task_id=str(task["task_id"]),
        role=role,
        parts=[a2a.Part(text=text_from_event(event))],
    )


def _pending_message(
    task: dict[str, Any], events: list[dict[str, Any]], requests: list[dict[str, Any]]
) -> a2a.Message | None:
    pending = [row for row in requests if row["status"] != "resolved"]
    if not pending:
        return None
    by_id = {str(event.get("id")): event for event in events}
    payloads: list[dict[str, Any]] = []
    for row in pending:
        source = by_id.get(str(row["cma_event_id"]))
        if source is None:
            continue
        if row.get("kind") == "clarification":
            tool_input = source.get("input") or {}
            payloads.append(
                ClarificationRequest(
                    request_id=str(row["request_id"]),
                    prompt=str(tool_input["prompt"]),
                    choices=tool_input.get("choices"),
                ).model_dump(by_alias=True, exclude_none=True)
            )
        else:
            tool = ToolDescriptor(name=str(source.get("name") or "tool"), arguments=source.get("input") or {})
            payloads.append(
                ToolApprovalRequest(request_id=str(row["request_id"]), tool=tool).model_dump(by_alias=True)
            )
    if not payloads:
        return None
    message = a2a.Message(
        message_id=f"input-{task['task_id']}",
        context_id=str(task["context_id"]),
        task_id=str(task["task_id"]),
        role=a2a.Role.ROLE_AGENT,
        parts=[a2a.Part(text="Approval required")],
        extensions=[HUMAN_INPUT_EXTENSION_URI],
    )
    message.metadata.update({HUMAN_INPUT_EXTENSION_URI: {"requests": payloads}})
    return message


def build_task(
    task: dict[str, Any],
    events: list[dict[str, Any]],
    requests: list[dict[str, Any]],
    options: TaskViewOptions | None = None,
    pending_text: str | None = None,
) -> a2a.Task:
    options = options or TaskViewOptions()
    start_id = task.get("cma_input_event_id")
    end_id = task.get("cma_terminal_event_id")
    in_slice = False
    full_history: list[a2a.Message] = []
    if not start_id and pending_text is not None:
        full_history.append(
            a2a.Message(
                message_id=str(task["message_id"]),
                context_id=str(task["context_id"]),
                task_id=str(task["task_id"]),
                role=a2a.Role.ROLE_USER,
                parts=[a2a.Part(text=pending_text)],
            )
        )
    for event in events:
        if not in_slice and start_id and event.get("id") == start_id:
            in_slice = True
        if in_slice and event.get("type") in {"user.message", "agent.message"} and event.get("id"):
            full_history.append(_message(task, event))
        if in_slice and end_id and event.get("id") == end_id:
            break
    history = full_history
    if options.history_length is not None:
        history = full_history[-options.history_length :] if options.history_length > 0 else []
    state = _STATES[str(task["a2a_state"])]
    result = a2a.Task(
        id=str(task["task_id"]),
        context_id=str(task["context_id"]),
        status=a2a.TaskStatus(state=state),
        history=history,
    )
    if state == a2a.TaskState.TASK_STATE_COMPLETED:
        for message in reversed(full_history):
            if message.role == a2a.Role.ROLE_AGENT:
                result.status.message.CopyFrom(message)
                break
    elif state == a2a.TaskState.TASK_STATE_INPUT_REQUIRED:
        pending_message = _pending_message(task, events, requests) if options.human_input_enabled else None
        if pending_message:
            result.status.message.CopyFrom(pending_message)
        else:
            result.status.message.CopyFrom(
                a2a.Message(
                    message_id=f"input-{task['task_id']}",
                    context_id=str(task["context_id"]),
                    task_id=str(task["task_id"]),
                    role=a2a.Role.ROLE_AGENT,
                    parts=[a2a.Part(text="This task requires user input.")],
                )
            )
    elif state == a2a.TaskState.TASK_STATE_REJECTED:
        reason = (
            "This context is unavailable after a provider failure."
            if task.get("held_reason") == "context-failed"
            else "Resolve the pending input request before starting another turn."
        )
        result.status.message.CopyFrom(
            a2a.Message(
                message_id=f"rejected-{task['task_id']}",
                context_id=str(task["context_id"]),
                task_id=str(task["task_id"]),
                role=a2a.Role.ROLE_AGENT,
                parts=[a2a.Part(text=reason)],
            )
        )
    elif state == a2a.TaskState.TASK_STATE_FAILED:
        terminal = next((event for event in events if event.get("id") == end_id), {})
        budget_reached = (
            terminal.get("type") == "session.budget_reached"
            or (terminal.get("stop_reason") or {}).get("type") == "budget_reached"
        )
        reason = (
            "The CMA session reached its budget before this task completed."
            if budget_reached
            else "The CMA session ended before this task completed."
        )
        result.status.message.CopyFrom(
            a2a.Message(
                message_id=f"failed-{task['task_id']}",
                context_id=str(task["context_id"]),
                task_id=str(task["task_id"]),
                role=a2a.Role.ROLE_AGENT,
                parts=[a2a.Part(text=reason)],
            )
        )
    return result
