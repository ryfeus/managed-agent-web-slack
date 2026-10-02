from unittest.mock import AsyncMock, MagicMock

import pytest
from a2a.types import a2a_pb2 as a2a

from managed_agents_app.agent_control_plane.service import ThreadAgentService


@pytest.mark.asyncio
async def test_stop_chooses_earliest_active_task_across_states() -> None:
    service = ThreadAgentService(MagicMock(), MagicMock(), "http://sink.test/events")
    completed = a2a.Task(id="completed", status=a2a.TaskStatus(state=a2a.TaskState.TASK_STATE_COMPLETED))
    waiting = a2a.Task(id="waiting", status=a2a.TaskStatus(state=a2a.TaskState.TASK_STATE_INPUT_REQUIRED))
    working = a2a.Task(id="working", status=a2a.TaskStatus(state=a2a.TaskState.TASK_STATE_WORKING))
    service.list_tasks = AsyncMock(return_value=[completed, waiting, working])  # type: ignore[method-assign]
    service.cancel_task = AsyncMock(return_value=waiting)  # type: ignore[method-assign]

    assert await service.cancel_active_task("thread-1") == waiting
    service.cancel_task.assert_awaited_once_with("thread-1", "waiting")


@pytest.mark.asyncio
async def test_active_task_refetches_earliest_task_with_human_input() -> None:
    service = ThreadAgentService(MagicMock(), MagicMock(), "http://sink.test/events")
    waiting = a2a.Task(id="waiting", status=a2a.TaskStatus(state=a2a.TaskState.TASK_STATE_INPUT_REQUIRED))
    queued = a2a.Task(id="queued", status=a2a.TaskStatus(state=a2a.TaskState.TASK_STATE_SUBMITTED))
    detailed = a2a.Task(id="waiting", status=waiting.status)
    service.list_tasks = AsyncMock(return_value=[waiting, queued])  # type: ignore[method-assign]
    service.get_task = AsyncMock(return_value=detailed)  # type: ignore[method-assign]

    assert await service.get_active_task("thread-1") is waiting
    assert await service.get_active_task("thread-1", human_input=True) is detailed
    service.get_task.assert_awaited_once_with("thread-1", "waiting", human_input=True)
