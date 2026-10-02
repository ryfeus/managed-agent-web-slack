"""Official A2A HTTP+JSON handler backed by durable controller state."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import suppress
from time import monotonic

from a2a.server.context import ServerCallContext
from a2a.server.events.event_queue import Event
from a2a.server.request_handlers.request_handler import RequestHandler
from a2a.types import a2a_pb2 as a2a
from a2a.utils.errors import (
    ExtendedAgentCardNotConfiguredError,
    InvalidParamsError,
    PushNotificationNotSupportedError,
)

from managed_agents_app.cma_controller.push_config import PushConfigService
from managed_agents_app.cma_controller.service import ControllerService
from managed_agents_app.cma_controller.task_view import TaskViewOptions
from managed_agents_app.protocols.controller_profile import HUMAN_INPUT_EXTENSION_URI

_TERMINAL = {
    a2a.TaskState.TASK_STATE_COMPLETED,
    a2a.TaskState.TASK_STATE_FAILED,
    a2a.TaskState.TASK_STATE_CANCELED,
    a2a.TaskState.TASK_STATE_REJECTED,
    a2a.TaskState.TASK_STATE_INPUT_REQUIRED,
}


class CmaA2ARequestHandler(RequestHandler):
    def __init__(
        self, service: ControllerService, card: a2a.AgentCard, push: PushConfigService | None = None
    ) -> None:
        self.service = service
        self.card = card
        self.push = push

    def _push(self) -> PushConfigService:
        if self.push is None:
            raise PushNotificationNotSupportedError()
        return self.push

    @staticmethod
    def _snapshot(full: a2a.Task, options: TaskViewOptions) -> a2a.Task:
        result = a2a.Task()
        result.CopyFrom(full)
        if options.history_length is not None:
            selected = list(full.history[-options.history_length :]) if options.history_length > 0 else []
            result.ClearField("history")
            result.history.extend(selected)
        return result

    async def on_message_send(self, params: a2a.SendMessageRequest, context: ServerCallContext) -> a2a.Task:
        length = (
            params.configuration.history_length if params.configuration.HasField("history_length") else None
        )
        options = self._options(context, length)
        full = await self.service.send(
            params.message, options=TaskViewOptions(enabled_extensions=options.enabled_extensions)
        )
        task = self._snapshot(full, options)
        if params.configuration.HasField("task_push_notification_config"):
            await self._push().create(params.configuration.task_push_notification_config, task.id)
        if params.configuration.return_immediately or task.status.state in _TERMINAL:
            return task
        async for event in self._watch(task, options, full):
            if isinstance(event, a2a.TaskStatusUpdateEvent) and event.status.state in _TERMINAL:
                return await self.service.get_task(task.id, options=options)
        return await self.service.get_task(task.id, options=options)

    @staticmethod
    def _options(context: ServerCallContext, history_length: int | None = None) -> TaskViewOptions:
        enabled = frozenset(context.requested_extensions & {HUMAN_INPUT_EXTENSION_URI})
        return TaskViewOptions(history_length=history_length, enabled_extensions=enabled)

    async def on_message_send_stream(
        self, params: a2a.SendMessageRequest, context: ServerCallContext
    ) -> AsyncGenerator[Event]:
        length = (
            params.configuration.history_length if params.configuration.HasField("history_length") else None
        )
        options = self._options(context, length)
        full = await self.service.send(
            params.message, options=TaskViewOptions(enabled_extensions=options.enabled_extensions)
        )
        task = self._snapshot(full, options)
        if params.configuration.HasField("task_push_notification_config"):
            await self._push().create(params.configuration.task_push_notification_config, task.id)
        async for event in self._watch(task, options, full):
            yield event

    async def on_get_task(self, params: a2a.GetTaskRequest, context: ServerCallContext) -> a2a.Task:
        length = params.history_length if params.HasField("history_length") else None
        return await self.service.get_task(params.id, options=self._options(context, length))

    async def on_list_tasks(
        self, params: a2a.ListTasksRequest, context: ServerCallContext
    ) -> a2a.ListTasksResponse:
        try:
            after = int(params.page_token or "0")
        except ValueError as error:
            raise InvalidParamsError("Invalid page token") from error
        length = params.history_length if params.HasField("history_length") else None
        size = params.page_size or 50
        tasks, next_token, total = await self.service.list_tasks(
            params.context_id, after, size, length, options=self._options(context, length)
        )
        return a2a.ListTasksResponse(
            tasks=tasks, next_page_token=next_token, page_size=size, total_size=total
        )

    async def on_cancel_task(self, params: a2a.CancelTaskRequest, context: ServerCallContext) -> a2a.Task:
        return await self.service.cancel(params.id, options=self._options(context))

    async def on_subscribe_to_task(
        self, params: a2a.SubscribeToTaskRequest, context: ServerCallContext
    ) -> AsyncGenerator[Event]:
        options = self._options(context)
        full = await self.service.get_task(
            params.id, options=TaskViewOptions(enabled_extensions=options.enabled_extensions)
        )
        task = self._snapshot(full, options)
        async for event in self._watch(task, options, full):
            yield event

    async def _watch(
        self, initial: a2a.Task, options: TaskViewOptions, baseline: a2a.Task
    ) -> AsyncGenerator[Event]:
        yield initial
        if initial.status.state in _TERMINAL:
            return
        live_options = TaskViewOptions(enabled_extensions=options.enabled_extensions)
        emitted = {message.message_id for message in baseline.history if message.role == a2a.Role.ROLE_AGENT}
        last_status = initial.status.SerializeToString(deterministic=True)
        marker: tuple[object, str, str | None, str | None] | None = None
        hints: asyncio.Queue[bool] = asyncio.Queue(maxsize=32)
        stream: asyncio.Task[None] | None = None
        stream_session: str | None = None
        next_stream_at = 0.0
        next_snapshot_at = monotonic() + 5

        async def pump(session_id: str) -> None:
            try:
                async for event in self.service.provider.stream_events(session_id):
                    kind = str(event.get("type", ""))
                    if kind.startswith("agent.thinking"):
                        continue
                    if not hints.full():
                        hints.put_nowait(True)
            except Exception:
                # The durable snapshot path is the reconnect fallback.
                pass
            finally:
                if not hints.full():
                    hints.put_nowait(True)

        try:
            while True:
                now = monotonic()
                current_marker = await self.service.watch_marker(initial.id)
                session_id = current_marker[3] if current_marker[2] else None
                if session_id and (stream is None or stream.done()) and now >= next_stream_at:
                    stream_session = session_id
                    stream = asyncio.create_task(pump(session_id))
                    next_stream_at = now + 2
                elif stream is not None and stream_session != session_id:
                    stream.cancel()
                    stream = None
                    stream_session = None
                changed = current_marker != marker
                hinted = False
                if not changed and now < next_snapshot_at:
                    with suppress(TimeoutError):
                        hinted = await asyncio.wait_for(hints.get(), timeout=1)
                    current_marker = await self.service.watch_marker(initial.id)
                    changed = current_marker != marker
                if not (changed or hinted or monotonic() >= next_snapshot_at):
                    continue
                current = await self.service.get_task(initial.id, options=live_options)
                for message in current.history:
                    if message.role == a2a.Role.ROLE_AGENT and message.message_id not in emitted:
                        emitted.add(message.message_id)
                        yield message
                status = current.status.SerializeToString(deterministic=True)
                if status != last_status:
                    yield a2a.TaskStatusUpdateEvent(
                        task_id=current.id, context_id=current.context_id, status=current.status
                    )
                    last_status = status
                marker = current_marker
                next_snapshot_at = monotonic() + 5
                if current.status.state in _TERMINAL:
                    return
        finally:
            if stream is not None:
                stream.cancel()
                with suppress(asyncio.CancelledError):
                    await stream

    async def on_create_task_push_notification_config(
        self, params: a2a.TaskPushNotificationConfig, context: ServerCallContext
    ) -> a2a.TaskPushNotificationConfig:
        return await self._push().create(params)

    async def on_get_task_push_notification_config(
        self, params: a2a.GetTaskPushNotificationConfigRequest, context: ServerCallContext
    ) -> a2a.TaskPushNotificationConfig:
        return await self._push().get(params.task_id, params.id)

    async def on_list_task_push_notification_configs(
        self, params: a2a.ListTaskPushNotificationConfigsRequest, context: ServerCallContext
    ) -> a2a.ListTaskPushNotificationConfigsResponse:
        return await self._push().list(params.task_id, params.page_size, params.page_token)

    async def on_delete_task_push_notification_config(
        self, params: a2a.DeleteTaskPushNotificationConfigRequest, context: ServerCallContext
    ) -> None:
        await self._push().delete(params.task_id, params.id)

    async def on_get_extended_agent_card(
        self, params: a2a.GetExtendedAgentCardRequest, context: ServerCallContext
    ) -> a2a.AgentCard:
        raise ExtendedAgentCardNotConfiguredError()
