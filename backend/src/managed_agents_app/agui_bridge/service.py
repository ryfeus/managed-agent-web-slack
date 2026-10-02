"""One AG-UI run over a durable A2A Task."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import suppress

from a2a.types import a2a_pb2 as a2a
from ag_ui.core import MessagesSnapshotEvent, RunErrorEvent, RunStartedEvent
from ag_ui.encoder import EventEncoder

from managed_agents_app.agent_control_plane.service import ThreadAgentService
from managed_agents_app.agui_bridge.history import canonical_history
from managed_agents_app.agui_bridge.mapper import FINAL_STATES, agent_message_events, finish_events


async def stream_task(
    service: ThreadAgentService,
    thread_id: str,
    run_id: str,
    task_id: str,
    *,
    snapshot: bool,
    await_resume_progress: bool = False,
    accept: str | None = None,
) -> AsyncIterator[str]:
    encoder = EventEncoder(accept or "text/event-stream")
    yield encoder.encode(RunStartedEvent(thread_id=thread_id, run_id=run_id))
    seen: set[str] = set()
    if snapshot:
        history = await canonical_history(service, thread_id)
        messages = history["messages"]
        if await_resume_progress:
            messages = [message for message in messages if message.get("id") != f"pending-{task_id}"]
        yield encoder.encode(MessagesSnapshotEvent(messages=messages))
        seen = {str(message["id"]) for message in messages if message.get("role") == "assistant"}
    failures = 0
    last_heartbeat = time.monotonic()
    while True:
        try:
            current = await service.get_task(thread_id, task_id, human_input=True)
            for message in current.history:
                if message.role == a2a.Role.ROLE_AGENT and message.message_id not in seen:
                    seen.add(message.message_id)
                    for event in agent_message_events(message):
                        yield encoder.encode(event)
            if await_resume_progress and current.status.state == a2a.TaskState.TASK_STATE_INPUT_REQUIRED:
                # Human-input admission is durable before the controller scheduler
                # moves the Task out of INPUT_REQUIRED. Do not finish this resumed
                # run with the previous interrupt snapshot.
                if time.monotonic() - last_heartbeat >= 15:
                    yield ": heartbeat\n\n"
                    last_heartbeat = time.monotonic()
                await asyncio.sleep(0.25)
                continue
            await_resume_progress = False
            if current.status.state in FINAL_STATES:
                for event in finish_events(current, thread_id, run_id):
                    yield encoder.encode(event)
                return

            queue: asyncio.Queue[a2a.StreamResponse | BaseException | None] = asyncio.Queue()

            async def pump(output: asyncio.Queue[a2a.StreamResponse | BaseException | None]) -> None:
                try:
                    async for item in service.subscribe_task(thread_id, task_id, human_input=True):
                        await output.put(item)
                except BaseException as error:
                    await output.put(error)
                finally:
                    await output.put(None)

            worker = asyncio.create_task(pump(queue))
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    if item is None or isinstance(item, BaseException):
                        if isinstance(item, asyncio.CancelledError):
                            raise item
                        break
                    payload = item.WhichOneof("payload")
                    if payload == "message":
                        message = item.message
                        if message.role == a2a.Role.ROLE_AGENT and message.message_id not in seen:
                            seen.add(message.message_id)
                            for event in agent_message_events(message):
                                yield encoder.encode(event)
                    if payload == "task" and item.task.status.state in FINAL_STATES:
                        break
                    if payload == "status_update" and item.status_update.status.state in FINAL_STATES:
                        break
            finally:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
            failures = 0
            # Always read canonical state after a stream ends. The pinned A2A
            # client can close after one agent Message while the Task still works.
        except asyncio.CancelledError:
            raise
        except Exception:
            failures += 1
            if failures >= 5:
                yield encoder.encode(
                    RunErrorEvent(message="The task stream is temporarily unavailable. Reload to reconnect.")
                )
                return
            await asyncio.sleep(min(failures, 3))
        else:
            await asyncio.sleep(0.25)
