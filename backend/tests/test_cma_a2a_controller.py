from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from a2a.types import a2a_pb2 as a2a

from managed_agents_app.cma_controller.app import agent_card
from managed_agents_app.cma_controller.pending_input import FilePendingInputStore
from managed_agents_app.cma_controller.pending_input_s3 import S3PendingInputStore
from managed_agents_app.cma_controller.scheduler import CmaScheduler
from managed_agents_app.cma_controller.service import ControllerService
from managed_agents_app.cma_controller.task_view import TaskViewOptions, build_task
from managed_agents_app.cma_controller.trigger import (
    AsyncLocalSchedulerTrigger,
    LocalSchedulerTrigger,
    SqsSchedulerTrigger,
)
from managed_agents_app.protocols.controller_profile import HUMAN_INPUT_EXTENSION_URI


def test_busy_scheduler_wakeup_uses_queue_failure_recovery(config, monkeypatch):
    # a webhook racing claim release receives ordinary SQS visibility recovery.
    from managed_agents_app.cma_controller import scheduler_handler
    from managed_agents_app.cma_controller.scheduler import SchedulerBusy

    configured = config.model_copy(
        update={
            "cma_pending_input_bucket": "fixture-input",
            "cma_scheduler_queue_url": "fixture-queue",
        }
    )
    scheduler = SimpleNamespace(run_once=AsyncMock(side_effect=SchedulerBusy("context-1")))
    monkeypatch.setattr(scheduler_handler, "load_config", lambda: configured)
    monkeypatch.setattr(
        scheduler_handler, "build_controller", lambda *_args, **_kwargs: SimpleNamespace(scheduler=scheduler)
    )
    event = {"Records": [{"messageId": "wakeup-1", "body": '{"version":1,"contextId":"context-1"}'}]}
    context = SimpleNamespace(get_remaining_time_in_millis=lambda: 54000)
    assert scheduler_handler.handler(event, context) == {
        "batchItemFailures": [{"itemIdentifier": "wakeup-1"}]
    }
    scheduler.run_once.assert_awaited_once_with("context-1", remaining_seconds=54)


def _task(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "task_id": "task_1",
        "context_id": "context_1",
        "message_id": "msg_1",
        "a2a_state": "COMPLETED",
        "cma_input_event_id": "user_1",
        "cma_terminal_event_id": "idle_1",
    }
    row.update(changes)
    return row


def test_task_view_rebuilds_bounded_history_without_thinking_or_later_turns() -> None:
    events = [
        {"id": "user_1", "type": "user.message", "content": [{"type": "text", "text": "Hello"}]},
        {"id": "thought", "type": "agent.thinking", "content": [{"type": "text", "text": "secret"}]},
        {"id": "agent_1", "type": "agent.message", "content": [{"type": "text", "text": "Hi"}]},
        {"id": "idle_1", "type": "session.status_idle", "stop_reason": {"type": "end_turn"}},
        {"id": "user_2", "type": "user.message", "content": [{"type": "text", "text": "Next"}]},
        {"id": "agent_2", "type": "agent.message", "content": [{"type": "text", "text": "Later"}]},
    ]
    full = build_task(_task(), events, [])
    assert [message.message_id for message in full.history] == ["msg_1", "cma-agent_1"]
    assert full.status.message.message_id == "cma-agent_1"
    assert full.status.message.parts[0].text == "Hi"
    assert list(build_task(_task(), events, [], TaskViewOptions(history_length=0)).history) == []
    assert [
        message.message_id
        for message in build_task(_task(), events, [], TaskViewOptions(history_length=1)).history
    ] == ["cma-agent_1"]
    assert len(build_task(_task(), events, [], TaskViewOptions(history_length=10)).history) == 2


def test_machine_approvals_are_not_exposed_as_human_input() -> None:
    events = [
        {"id": "user_1", "type": "user.message", "content": [{"type": "text", "text": "Run"}]},
        {"id": "tool_1", "type": "agent.tool_use", "name": "browser", "input": {"url": "example"}},
        {"id": "tool_2", "type": "agent.mcp_tool_use", "name": "deploy", "input": {}},
    ]
    requests = [
        {"request_id": "approval-tool_1", "cma_event_id": "tool_1", "status": "pending"},
        {"request_id": "approval-tool_2", "cma_event_id": "tool_2", "status": "pending"},
    ]
    task = build_task(
        _task(a2a_state="INPUT_REQUIRED", cma_terminal_event_id=None),
        events,
        requests,
        TaskViewOptions(enabled_extensions=frozenset({HUMAN_INPUT_EXTENSION_URI})),
    )
    assert task.status.state == a2a.TaskState.TASK_STATE_INPUT_REQUIRED
    assert not task.status.message.extensions
    assert "requests" not in task.status.message.metadata


@pytest.mark.parametrize(
    ("terminal", "expected"),
    [
        (
            {"id": "idle_1", "type": "session.budget_reached", "error": "private token"},
            "The CMA session reached its budget before this task completed.",
        ),
        (
            {"id": "idle_1", "type": "session.status_terminated", "error": "private token"},
            "The CMA session ended before this task completed.",
        ),
    ],
)
def test_failed_task_has_stable_safe_status_message(terminal: dict[str, object], expected: str) -> None:
    task = build_task(_task(a2a_state="FAILED"), [terminal], [])
    assert task.status.state == a2a.TaskState.TASK_STATE_FAILED
    assert task.status.message.message_id == "failed-task_1"
    assert task.status.message.parts[0].text == expected
    assert "private token" not in str(task)


def test_agent_card_advertises_phase_two_capabilities() -> None:
    card = agent_card("http://localhost:8081")
    assert card.supported_interfaces[0].protocol_binding == "HTTP+JSON"
    assert card.capabilities.streaming
    assert not card.capabilities.push_notifications
    assert [extension.uri for extension in card.capabilities.extensions] == [HUMAN_INPUT_EXTENSION_URI]


def test_pending_store_and_controller_only_queue(tmp_path: Path) -> None:
    store = FilePendingInputStore(tmp_path)
    store.put("opaque", "private prompt")
    assert store.get("opaque") == "private prompt"
    assert (tmp_path / "opaque").stat().st_mode & 0o077 == 0
    store.delete("opaque")
    assert store.list_keys() == []
    with pytest.raises(ValueError):
        store.put("../escape", "text")
    local = LocalSchedulerTrigger()
    local.schedule("context_1", "task-admitted")
    assert local.pending == [("context_1", "task-admitted", 0)]
    sqs = MagicMock()
    SqsSchedulerTrigger("queue-url", sqs).schedule("context_1", "task-admitted")
    assert sqs.send_message.call_args.kwargs["MessageBody"] == (
        '{"version": 1, "contextId": "context_1", "reason": "task-admitted"}'
    )


@pytest.mark.asyncio
async def test_local_scheduler_shutdown_cancels_delayed_wakeups() -> None:
    trigger = AsyncLocalSchedulerTrigger()
    scheduler = MagicMock()
    scheduler.run_once = AsyncMock()
    trigger.start(scheduler)
    trigger.schedule("context_1", "delayed", 60)
    await asyncio.sleep(0)
    await trigger.stop()
    scheduler.run_once.assert_not_awaited()


def test_s3_store_requests_server_side_encryption() -> None:
    client = MagicMock()
    client.get_object.return_value = {"Body": BytesIO(b"prompt")}
    store = S3PendingInputStore("private-bucket", client)
    store.put("opaque", "prompt")
    assert client.put_object.call_args.kwargs["ServerSideEncryption"] == "AES256"
    assert client.put_object.call_args.kwargs["Key"] == "pending/opaque"
    assert store.get("opaque") == "prompt"
    client.get_paginator.return_value.paginate.return_value = [
        {"Contents": [{"Key": "pending/old", "LastModified": datetime(2020, 1, 1, tzinfo=UTC)}]}
    ]
    assert store.list_old_keys(86400) == ["old"]


def test_orphan_sweep_preserves_referenced_pending_input(tmp_path: Path) -> None:
    store = FilePendingInputStore(tmp_path)
    store.put("referenced", "still pending")
    store.put("orphan", "not admitted")
    old = datetime(2020, 1, 1, tzinfo=UTC).timestamp()
    os.utime(tmp_path / "referenced", (old, old))
    os.utime(tmp_path / "orphan", (old, old))
    repository = MagicMock()
    repository.pending_input_keys.return_value = {"referenced"}
    repository.stale_pending_input_count.return_value = 1
    scheduler = CmaScheduler(repository, MagicMock(), store, LocalSchedulerTrigger())
    assert scheduler.sweep_orphans() == (1, 1)
    assert store.list_keys() == ["referenced"]


@pytest.mark.asyncio
async def test_cancel_does_not_delete_input_if_queued_task_became_active() -> None:
    repository = MagicMock()
    repository.get_task.return_value = {
        "task_id": "task_1",
        "context_id": "context_1",
        "internal_state": "queued",
        "input_object_key": "opaque",
    }
    repository.cancel_task.return_value = {
        "task_id": "task_1",
        "context_id": "context_1",
        "internal_state": "dispatching",
        "a2a_state": "SUBMITTED",
    }
    pending = MagicMock()
    trigger = MagicMock()
    service = ControllerService(repository, MagicMock(), pending, trigger)
    service.get_task = AsyncMock(return_value=a2a.Task(id="task_1", context_id="context_1"))

    await service.cancel("task_1")

    pending.delete.assert_not_called()
    trigger.schedule.assert_called_once_with("context_1", "cancel-requested")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary", [a2a.TaskState.TASK_STATE_COMPLETED, a2a.TaskState.TASK_STATE_INPUT_REQUIRED]
)
async def test_watch_reads_periodic_history_without_marker_change_or_cma_sse(monkeypatch, boundary):
    from managed_agents_app.cma_controller import request_handler

    clock = [0.0]
    reads = []
    initial = a2a.Task(
        id="task-1", context_id="context-1", status=a2a.TaskStatus(state=a2a.TaskState.TASK_STATE_WORKING)
    )

    async def sleep(seconds):
        clock[0] += seconds

    async def get_task(*_args, **_kwargs):
        reads.append(clock[0])
        current = a2a.Task()
        current.CopyFrom(initial)
        if clock[0] >= 2:
            current.history.append(
                a2a.Message(
                    message_id="stable", role=a2a.Role.ROLE_AGENT, parts=[a2a.Part(text="Persisted answer")]
                )
            )
        if clock[0] >= 7:
            current.status.state = boundary
        return current

    async def marker(*_args):
        return (7 if clock[0] >= 7 else 0, "state", None, None)

    service = MagicMock()
    service.get_task = AsyncMock(side_effect=get_task)
    service.watch_marker = AsyncMock(side_effect=marker)
    service.provider.stream_events.side_effect = AssertionError("CMA SSE is P1-only")
    monkeypatch.setattr(request_handler, "monotonic", lambda: clock[0])
    monkeypatch.setattr(request_handler, "asyncio", SimpleNamespace(sleep=sleep))
    handler = request_handler.CmaA2ARequestHandler(service, agent_card("http://a2a.test"))
    observed = [event async for event in handler._watch(initial, TaskViewOptions(), initial)]
    assert reads == [0, 5, 7]  # Complete message is observed while marker/state still says WORKING.
    assert [type(event) for event in observed] == [a2a.Task, a2a.Message, a2a.TaskStatusUpdateEvent]
    assert observed[1].message_id == "stable"
    assert observed[2].status.state == boundary
    service.provider.stream_events.assert_not_called()
    service.cancel.assert_not_called()


@pytest.mark.asyncio
async def test_watch_deadline_does_not_complete_or_cancel_work(monkeypatch):
    from managed_agents_app.cma_controller import request_handler

    clock = [0.0]

    async def sleep(seconds):
        clock[0] += seconds

    current = a2a.Task(id="task-1", status=a2a.TaskStatus(state=a2a.TaskState.TASK_STATE_WORKING))
    service = MagicMock()
    service.get_task = AsyncMock(return_value=current)
    service.watch_marker = AsyncMock(return_value=(0, "WORKING", None, None))
    monkeypatch.setattr(request_handler, "monotonic", lambda: clock[0])
    monkeypatch.setattr(request_handler, "asyncio", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(request_handler, "_WATCH_SECONDS", 2)
    handler = request_handler.CmaA2ARequestHandler(service, agent_card("http://a2a.test"))
    assert [event async for event in handler._watch(current, TaskViewOptions(), current)] == [current]
    assert clock[0] == 2
    service.cancel.assert_not_called()
    service.provider.stream_events.assert_not_called()
