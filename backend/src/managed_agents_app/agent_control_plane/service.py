"""Bind logical application turns to controller-owned A2A tasks."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from a2a.types import a2a_pb2 as a2a

from managed_agents_app.a2a_client.client import A2AControllerClient, DefinitiveA2ARequestError
from managed_agents_app.db.thread_repository import ThreadRepository
from managed_agents_app.protocols.controller_profile import HUMAN_INPUT_EXTENSION_URI

ACTIVE_TASK_STATES = {
    a2a.TaskState.TASK_STATE_SUBMITTED,
    a2a.TaskState.TASK_STATE_WORKING,
    a2a.TaskState.TASK_STATE_INPUT_REQUIRED,
}


class RetryableControlPlaneError(RuntimeError):
    pass


class RetryablePushSetupError(RetryableControlPlaneError):
    def __init__(self, task_id: str) -> None:
        super().__init__(f"Push setup failed for accepted task {task_id}; retry the same client message ID")
        self.task_id = task_id


class ThreadAgentService:
    def __init__(
        self,
        repository: ThreadRepository,
        client: A2AControllerClient,
        event_sink_url: str,
        *,
        claim_seconds: int = 30,
    ) -> None:
        self.repository = repository
        self.client = client
        self.event_sink_url = event_sink_url.rstrip("/")
        self.claim_seconds = claim_seconds

    async def _thread(self, thread_id: str) -> dict[str, Any]:
        thread = await asyncio.to_thread(self.repository.get_thread, thread_id)
        if thread is None:
            raise KeyError("Unknown thread")
        return thread

    async def send_turn(self, thread_id: str, client_message_id: str, text: str) -> a2a.Task:
        if not text.strip() or not client_message_id:
            raise ValueError("A nonempty text and client message ID are required")
        thread = await self._thread(thread_id)
        agent_id = str(thread["agent_id"])
        await self.client.get_agent_card(agent_id)
        await asyncio.to_thread(self.repository.record_logical_send, thread_id, agent_id, client_message_id)
        context_id = str(thread["context_id"]) if thread["context_id"] else ""
        claim_id = ""
        if not context_id:
            for attempt in range(11):
                claim = await asyncio.to_thread(
                    self.repository.claim_context_initialization, thread_id, self.claim_seconds
                )
                if claim.status == "acquired":
                    claim_id = str(claim.claim_id)
                    break
                if claim.status == "ready":
                    context_id = str(claim.context_id)
                    break
                if attempt < 10:
                    await asyncio.sleep(0.2)
            if not context_id and not claim_id:
                raise RetryableControlPlaneError("Thread context initialization is busy")
        message = a2a.Message(
            message_id=client_message_id,
            context_id=context_id,
            role=a2a.Role.ROLE_USER,
            parts=[a2a.Part(text=text)],
        )
        # A failed response can follow durable task admission. Keep the first-turn
        # claim until its lease expires, then retry with the same messageId.
        try:
            task = await self.client.send_message(agent_id, message)
        except DefinitiveA2ARequestError:
            if claim_id:
                await asyncio.to_thread(self.repository.release_context_initialization, thread_id, claim_id)
            raise
        if claim_id:
            await asyncio.to_thread(self.repository.bind_context, thread_id, claim_id, task.context_id)
        elif task.context_id != context_id:
            raise RuntimeError("Controller changed the bound context")
        await asyncio.to_thread(self.repository.bind_task_id, thread_id, client_message_id, task.id)
        try:
            await self.client.create_push_config(
                agent_id, task.id, "app-control-plane", self.event_sink_url + "/" + agent_id
            )
        except Exception as error:
            raise RetryablePushSetupError(task.id) from error
        return task

    async def _bound(self, thread_id: str, task_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        thread = await self._thread(thread_id)
        binding = await asyncio.to_thread(self.repository.get_task_binding, str(thread["agent_id"]), task_id)
        if binding is None or str(binding["thread_id"]) != thread_id:
            raise KeyError("Task is not bound to this thread")
        return thread, binding

    async def get_task(self, thread_id: str, task_id: str, *, human_input: bool = False) -> a2a.Task:
        thread, _ = await self._bound(thread_id, task_id)
        task = await self.client.get_task(str(thread["agent_id"]), task_id, human_input=human_input)
        if task.context_id != thread["context_id"]:
            raise RuntimeError("Controller Task context does not match thread")
        return task

    async def cancel_task(self, thread_id: str, task_id: str) -> a2a.Task:
        thread, _ = await self._bound(thread_id, task_id)
        return await self.client.cancel_task(str(thread["agent_id"]), task_id)

    async def continue_human_input(
        self, thread_id: str, task_id: str, client_message_id: str, response: dict[str, Any]
    ) -> a2a.Task:
        thread, _ = await self._bound(thread_id, task_id)
        message = a2a.Message(
            message_id=client_message_id,
            context_id=str(thread["context_id"]),
            task_id=task_id,
            role=a2a.Role.ROLE_USER,
            extensions=[HUMAN_INPUT_EXTENSION_URI],
        )
        message.metadata.update({HUMAN_INPUT_EXTENSION_URI: {"response": response}})
        return await self.client.send_message(str(thread["agent_id"]), message, human_input=True)

    async def subscribe_task(
        self, thread_id: str, task_id: str, *, human_input: bool = False
    ) -> AsyncIterator[a2a.StreamResponse]:
        thread, _ = await self._bound(thread_id, task_id)
        async for response in self.client.subscribe_task(
            str(thread["agent_id"]), task_id, human_input=human_input
        ):
            yield response

    async def list_tasks(self, thread_id: str) -> list[a2a.Task]:
        thread = await self._thread(thread_id)
        if not thread["context_id"]:
            return []
        tasks: list[a2a.Task] = []
        page_token = ""
        while True:
            page = await self.client.list_tasks(
                str(thread["agent_id"]), str(thread["context_id"]), page_token=page_token
            )
            tasks.extend(page.tasks)
            if not page.next_page_token:
                return tasks
            page_token = page.next_page_token

    async def get_active_task(self, thread_id: str, *, human_input: bool = False) -> a2a.Task | None:
        tasks = await self.list_tasks(thread_id)
        active = next((task for task in tasks if task.status.state in ACTIVE_TASK_STATES), None)
        if active is not None and human_input:
            return await self.get_task(thread_id, active.id, human_input=True)
        return active

    async def cancel_active_task(self, thread_id: str) -> a2a.Task | None:
        active = await self.get_active_task(thread_id)
        return await self.cancel_task(thread_id, active.id) if active else None
