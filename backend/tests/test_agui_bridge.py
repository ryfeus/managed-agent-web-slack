"""Provider-neutral AG-UI wire mapping and bridge authorization."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from a2a.types import a2a_pb2 as a2a
from fastapi.testclient import TestClient

from managed_agents_app.agui_bridge.app import create_app
from managed_agents_app.agui_bridge.history import canonical_history
from managed_agents_app.agui_bridge.mapper import agent_message_events, finish_events, history_message
from managed_agents_app.protocols.controller_profile import HUMAN_INPUT_EXTENSION_URI

THREAD_ID = "03cb1122-146c-4dab-8393-1573ee38e195"


def test_agent_message_and_task_lifecycle_mapping() -> None:
    message = a2a.Message(
        message_id="a2a-final-1",
        role=a2a.Role.ROLE_AGENT,
        parts=[a2a.Part(text="Hello"), a2a.Part(text="world")],
    )
    events = agent_message_events(message)
    assert [event.type for event in events] == [
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
    ]
    assert events[1].delta == "Hello\nworld"
    assert history_message(message).id == "a2a-final-1"
    task = a2a.Task(
        id="task-1",
        status=a2a.TaskStatus(state=a2a.TaskState.TASK_STATE_COMPLETED),
        history=[message],
    )
    finish = finish_events(task, THREAD_ID, "browser-run")
    assert finish[0].run_id == "browser-run"
    assert finish[0].outcome.type == "success"
    task.status.state = a2a.TaskState.TASK_STATE_CANCELED
    assert finish_events(task, THREAD_ID, "browser-run")[0].message == "The task was cancelled."
    task.status.state = a2a.TaskState.TASK_STATE_REJECTED
    assert finish_events(task, THREAD_ID, "browser-run")[0].type == "RUN_ERROR"


def test_auth_and_ownership_precede_a2a_access(monkeypatch, config) -> None:
    class Threads:
        def owns_thread(self, principal: str, thread_id: str) -> bool:
            return principal == "owner" and thread_id == THREAD_ID

    runtime = SimpleNamespace(config=config, threads=Threads())
    monkeypatch.setattr(
        "managed_agents_app.agui_bridge.app.principal_from_cookie",
        lambda cookie, secret: "owner" if cookie == "owner" else "other" if cookie == "other" else None,
    )
    client = TestClient(create_app(runtime))
    payload = {
        "threadId": THREAD_ID,
        "runId": "run-1",
        "state": {},
        "messages": [{"id": "user-1", "role": "user", "content": "hello"}],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    for method, path in [
        ("GET", f"/api/agui/threads/{THREAD_ID}/history"),
        ("GET", f"/api/agui/threads/{THREAD_ID}/resume"),
        ("POST", f"/api/agui/threads/{THREAD_ID}/cancel"),
        ("POST", "/api/agui"),
    ]:
        for cookie, expected in [(None, 401), ("other", 404)]:
            response = client.request(
                method,
                path,
                headers={"cookie": cookie} if cookie else {},
                content=json.dumps(payload) if path == "/api/agui" else None,
            )
            assert response.status_code == expected


@pytest.mark.asyncio
async def test_history_keeps_earliest_input_required_task_active() -> None:
    waiting = a2a.Task(
        id="waiting",
        status=a2a.TaskStatus(state=a2a.TaskState.TASK_STATE_INPUT_REQUIRED),
    )
    waiting.status.message.extensions.append(HUMAN_INPUT_EXTENSION_URI)
    waiting.status.message.metadata.update(
        {
            HUMAN_INPUT_EXTENSION_URI: {
                "requests": [
                    {
                        "kind": "tool_approval",
                        "requestId": "request-waiting",
                        "tool": {"name": "browser", "arguments": {"url": "https://example.com"}},
                        "allowedResponses": ["allow", "deny"],
                    }
                ]
            }
        }
    )
    queued = a2a.Task(id="queued", status=a2a.TaskStatus(state=a2a.TaskState.TASK_STATE_SUBMITTED))
    service = MagicMock()
    service.list_tasks = AsyncMock(return_value=[waiting, queued])
    service.get_task = AsyncMock(side_effect=[waiting, queued])
    service.get_active_task = AsyncMock(return_value=waiting)

    history = await canonical_history(service, THREAD_ID)

    assert history["activeTaskId"] == "waiting"
    assert history["activeTaskState"] == "TASK_STATE_INPUT_REQUIRED"
    assert history["hasPendingInterrupts"] is True
    assert history["messages"][-1]["id"] == "pending-waiting"
    assert history["messages"][-1]["metadata"]["custom"]["agui"]["interrupts"][0]["id"] == "request-waiting"
    service.get_active_task.assert_awaited_once_with(THREAD_ID, human_input=True)
