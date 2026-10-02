"""Decode provider-neutral human-input/v1 requests from an A2A Task."""

from __future__ import annotations

from a2a.types import a2a_pb2 as a2a
from google.protobuf.json_format import MessageToDict  # type: ignore[import-untyped]
from pydantic import TypeAdapter

from managed_agents_app.protocols.controller_profile import HUMAN_INPUT_EXTENSION_URI
from managed_agents_app.protocols.human_input import (
    ClarificationRequest,
    HumanInputRequest,
    ToolApprovalRequest,
)


def requests_from_task(task: a2a.Task) -> list[ToolApprovalRequest | ClarificationRequest]:
    if task.status.state != a2a.TaskState.TASK_STATE_INPUT_REQUIRED:
        return []
    message = task.status.message
    if HUMAN_INPUT_EXTENSION_URI not in message.extensions:
        return []
    metadata = MessageToDict(message.metadata, preserving_proto_field_name=True)
    envelope = metadata.get(HUMAN_INPUT_EXTENSION_URI, {})
    if not isinstance(envelope, dict):
        return []
    raw = envelope.get("requests", [])
    if not isinstance(raw, list):
        return []
    adapter: TypeAdapter[ToolApprovalRequest | ClarificationRequest] = TypeAdapter(HumanInputRequest)
    return [adapter.validate_python(item) for item in raw]
