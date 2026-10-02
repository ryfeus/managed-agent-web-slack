"""A2A admission and durable task reads, independent of HTTP transport."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress

from a2a.types import a2a_pb2 as a2a
from a2a.utils.errors import InvalidParamsError, TaskNotFoundError
from google.protobuf.json_format import MessageToDict  # type: ignore[import-untyped]
from pydantic import TypeAdapter, ValidationError

from managed_agents_app.cma_controller.pending_input import PendingInputStore
from managed_agents_app.cma_controller.provider import CmaProvider
from managed_agents_app.cma_controller.push_trigger import PushTrigger
from managed_agents_app.cma_controller.runtime_repository import ControllerRuntimeRepository
from managed_agents_app.cma_controller.task_view import TaskViewOptions, build_task
from managed_agents_app.cma_controller.trigger import SchedulerTrigger
from managed_agents_app.protocols.controller_profile import HUMAN_INPUT_EXTENSION_URI
from managed_agents_app.protocols.human_input import (
    ClarificationResponse,
    HumanInputResponse,
    ToolApprovalResponse,
)


class ControllerService:
    def __init__(
        self,
        repository: ControllerRuntimeRepository,
        provider: CmaProvider,
        pending_input: PendingInputStore,
        trigger: SchedulerTrigger,
        push_trigger: PushTrigger | None = None,
    ) -> None:
        self.repository = repository
        self.provider = provider
        self.pending_input = pending_input
        self.trigger = trigger
        self.push_trigger = push_trigger

    @staticmethod
    def _view_options(history_length: int | None, options: TaskViewOptions | None) -> TaskViewOptions:
        selected = options or TaskViewOptions(history_length=history_length)
        if selected.history_length is not None and selected.history_length < 0:
            raise InvalidParamsError("historyLength must be nonnegative")
        return selected

    async def send(
        self,
        message: a2a.Message,
        history_length: int | None = None,
        *,
        options: TaskViewOptions | None = None,
    ) -> a2a.Task:
        view = self._view_options(history_length, options)
        if message.role != a2a.Role.ROLE_USER or not message.message_id:
            raise InvalidParamsError("A USER message with messageId is required")
        if message.task_id:
            return await self._continue(message, view)
        if message.extensions:
            raise InvalidParamsError("Extensions must continue an existing task")
        if not message.parts or any(part.WhichOneof("content") != "text" for part in message.parts):
            raise InvalidParamsError("Only text parts are supported")
        body = "".join(part.text for part in message.parts)
        if not body.strip():
            raise InvalidParamsError("Text input cannot be empty")
        key = str(uuid.uuid4())
        await asyncio.to_thread(self.pending_input.put, key, body)
        try:
            context, task, created = await asyncio.to_thread(
                self.repository.admit,
                proposed_context_id=str(uuid.uuid4()),
                requested_context_id=message.context_id or None,
                message_id=message.message_id,
                proposed_task_id=str(uuid.uuid4()),
                input_object_key=key,
            )
        except KeyError as error:
            await asyncio.to_thread(self.pending_input.delete, key)
            raise InvalidParamsError("Unknown CMA context") from error
        except Exception:
            await asyncio.to_thread(self.pending_input.delete, key)
            raise
        if not created:
            await asyncio.to_thread(self.pending_input.delete, key)
        # Enqueue even on an idempotent retry: this closes a committed-admission/enqueue failure window.
        if task["internal_state"] != "terminal":
            await asyncio.to_thread(self.trigger.schedule, str(context["context_id"]), "task-admitted")
        return await self.get_task(str(task["task_id"]), options=view)

    async def _continue(self, message: a2a.Message, options: TaskViewOptions) -> a2a.Task:
        if not options.human_input_enabled or HUMAN_INPUT_EXTENSION_URI not in message.extensions:
            raise InvalidParamsError("Ordinary text cannot continue an existing task")
        if message.parts or set(message.extensions) != {HUMAN_INPUT_EXTENSION_URI}:
            raise InvalidParamsError("Human input must use only the declared extension")
        task = await asyncio.to_thread(self.repository.get_task, message.task_id)
        if task is None or task["context_id"] != message.context_id:
            raise TaskNotFoundError()
        metadata = MessageToDict(message.metadata, preserving_proto_field_name=True)
        envelope = metadata.get(HUMAN_INPUT_EXTENSION_URI)
        if not isinstance(envelope, dict) or "response" not in envelope:
            raise InvalidParamsError("Missing human-input/v1 response")
        try:
            response: ToolApprovalResponse | ClarificationResponse = TypeAdapter(
                HumanInputResponse
            ).validate_python(envelope["response"])
        except ValidationError as error:
            raise InvalidParamsError("Invalid human-input/v1 response") from error
        if isinstance(response, ClarificationResponse):
            prior = await asyncio.to_thread(
                self.repository.get_input_request, response.request_id, message.task_id
            )
            if prior is None or prior["kind"] != "clarification":
                raise InvalidParamsError("Unknown clarification request")
            if prior["status"] != "pending":
                return await self.get_task(message.task_id, options=options)
            if task["internal_state"] != "input_required":
                raise InvalidParamsError("Task is not waiting for human input")
            answer_key = str(uuid.uuid4())
            await asyncio.to_thread(self.pending_input.put, answer_key, response.answer)
            try:
                row = await asyncio.to_thread(
                    self.repository.decide_clarification_request,
                    response.request_id,
                    message.task_id,
                    answer_key,
                )
            except Exception:
                await asyncio.to_thread(self.pending_input.delete, answer_key)
                raise
            if row is None or row["answer_object_key"] != answer_key:
                await asyncio.to_thread(self.pending_input.delete, answer_key)
            if row is None:
                raise InvalidParamsError("Unknown clarification request")
            await asyncio.to_thread(self.trigger.schedule, message.context_id, "human-input")
            return await self.get_task(message.task_id, options=options)
        if task["internal_state"] != "input_required":
            prior = await asyncio.to_thread(
                self.repository.get_input_request, response.request_id, message.task_id
            )
            if prior and prior["decision"] != response.decision:
                raise InvalidParamsError("Input request was resolved with a different decision")
            if prior and prior["status"] != "pending" and prior["decision"] == response.decision:
                return await self.get_task(message.task_id, options=options)
            raise InvalidParamsError("Task is not waiting for human input")
        reason_key = str(uuid.uuid4()) if response.reason else None
        if reason_key:
            await asyncio.to_thread(self.pending_input.put, reason_key, response.reason or "")
        try:
            row = await asyncio.to_thread(
                self.repository.decide_input_request,
                response.request_id,
                message.task_id,
                response.decision,
                reason_key,
            )
        except Exception:
            if reason_key:
                await asyncio.to_thread(self.pending_input.delete, reason_key)
            raise
        if reason_key and (row is None or row["decision_reason_object_key"] != reason_key):
            await asyncio.to_thread(self.pending_input.delete, reason_key)
        if row is None:
            raise InvalidParamsError("Unknown input request")
        if row["decision"] != response.decision:
            raise InvalidParamsError("Input request was resolved with a different decision")
        await asyncio.to_thread(self.trigger.schedule, message.context_id, "human-input")
        return await self.get_task(message.task_id, options=options)

    async def get_task(
        self,
        task_id: str,
        history_length: int | None = None,
        *,
        options: TaskViewOptions | None = None,
    ) -> a2a.Task:
        view = self._view_options(history_length, options)
        task = await asyncio.to_thread(self.repository.get_task, task_id)
        if task is None:
            raise TaskNotFoundError()
        pending_text = None
        if not task.get("cma_input_event_id") and task.get("input_object_key"):
            key = str(task["input_object_key"])
            try:
                pending_text = await asyncio.to_thread(self.pending_input.get, key)
            except Exception:
                # The scheduler can bind the CMA event and remove the object between reads.
                refreshed = await asyncio.to_thread(self.repository.get_task, task_id)
                if refreshed is None or refreshed.get("input_object_key") == key:
                    raise
                task = refreshed
        context = await asyncio.to_thread(self.repository.get_context, str(task["context_id"]))
        if context is None:
            raise TaskNotFoundError()
        events = (
            await self.provider.list_events(str(context["cma_session_id"]))
            if context.get("cma_session_id")
            else []
        )
        requests = await asyncio.to_thread(self.repository.list_input_requests, task_id)
        return build_task(task, events, requests, view, pending_text)

    async def watch_marker(self, task_id: str) -> tuple[object, str, str | None, str | None]:
        """Read cheap durable state before deciding whether a full CMA replay is needed."""
        task = await asyncio.to_thread(self.repository.get_task, task_id)
        if task is None:
            raise TaskNotFoundError()
        context = await asyncio.to_thread(self.repository.get_context, str(task["context_id"]))
        if context is None:
            raise TaskNotFoundError()
        return (
            task.get("updated_at"),
            str(task["a2a_state"]),
            str(task["cma_input_event_id"]) if task.get("cma_input_event_id") else None,
            str(context["cma_session_id"]) if context.get("cma_session_id") else None,
        )

    async def list_tasks(
        self,
        context_id: str,
        after_sequence: int,
        page_size: int,
        history_length: int | None,
        *,
        options: TaskViewOptions | None = None,
    ) -> tuple[list[a2a.Task], str, int]:
        view = self._view_options(history_length, options)
        if not context_id:
            raise InvalidParamsError("contextId is required in Phase 2")
        if after_sequence < 0 or not 1 <= page_size <= 100:
            raise InvalidParamsError("Invalid task page")
        rows = await asyncio.to_thread(self.repository.list_tasks, context_id, after_sequence, page_size + 1)
        more = len(rows) > page_size
        selected = rows[:page_size]
        tasks = [await self.get_task(str(row["task_id"]), options=view) for row in selected]
        next_token = str(selected[-1]["sequence"]) if more and selected else ""
        total = await asyncio.to_thread(self.repository.count_tasks, context_id)
        return tasks, next_token, total

    async def cancel(self, task_id: str, *, options: TaskViewOptions | None = None) -> a2a.Task:
        before = await asyncio.to_thread(self.repository.get_task, task_id)
        if before is None:
            raise TaskNotFoundError()
        after = await asyncio.to_thread(self.repository.cancel_task, task_id)
        assert after is not None
        if after["internal_state"] != "terminal":
            await asyncio.to_thread(self.trigger.schedule, str(after["context_id"]), "cancel-requested")
        elif self.push_trigger is not None:
            with suppress(Exception):
                self.push_trigger.schedule_context(str(after["context_id"]), "queued-cancelled")
        return await self.get_task(task_id, options=options)
