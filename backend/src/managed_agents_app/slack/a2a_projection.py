"""Pure A2A Task to Slack projection decisions."""

from __future__ import annotations

from a2a.types import a2a_pb2 as a2a

from managed_agents_app.protocols.human_input_wire import requests_from_task


def agent_messages(task: a2a.Task) -> list[a2a.Message]:
    return [message for message in task.history if message.role == a2a.Role.ROLE_AGENT and message.message_id]


def message_text(message: a2a.Message) -> str:
    return "\n\n".join(
        str(part.text) for part in message.parts if part.WhichOneof("content") == "text" and part.text
    ).strip()


def task_state(task: a2a.Task) -> str:
    return str(a2a.TaskState.Name(task.status.state)).removeprefix("TASK_STATE_")


def task_slack_status(task: a2a.Task) -> str:
    state = task.status.state
    if state == a2a.TaskState.TASK_STATE_INPUT_REQUIRED:
        return "suspended"
    if state in {a2a.TaskState.TASK_STATE_SUBMITTED, a2a.TaskState.TASK_STATE_WORKING}:
        return "processing"
    return "active"


def is_settled(task: a2a.Task) -> bool:
    return task.status.state not in {
        a2a.TaskState.TASK_STATE_SUBMITTED,
        a2a.TaskState.TASK_STATE_WORKING,
    }


__all__ = [
    "agent_messages",
    "message_text",
    "requests_from_task",
    "task_state",
    "task_slack_status",
    "is_settled",
]
