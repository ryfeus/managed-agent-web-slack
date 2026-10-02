"""Private ASGI receiver for standard A2A push envelopes."""

from __future__ import annotations

import asyncio
import re

from a2a.types import a2a_pb2 as a2a
from fastapi import FastAPI, HTTPException, Request
from google.protobuf.json_format import Parse, ParseError  # type: ignore[import-untyped]

from managed_agents_app.a2a_client.client import A2AControllerClient
from managed_agents_app.a2a_event_sink.repository import EventSinkRepository, ReceiptConflict
from managed_agents_app.agents.registry import from_config
from managed_agents_app.config import load_config
from managed_agents_app.db.thread_repository import ThreadRepository
from managed_agents_app.domain import A2A_TASK_UPDATED, A2ATaskUpdated
from managed_agents_app.events import EventBridgeEventBus
from managed_agents_app.ports.events import EventBus
from managed_agents_app.protocols.controller_profile import A2A_DELIVERY_ID_HEADER

_DELIVERY_ID = re.compile(r"^[0-9a-f]{64}$")


def create_app(repository: EventSinkRepository, client: A2AControllerClient, bus: EventBus) -> FastAPI:
    app = FastAPI()

    @app.post("/internal/a2a/events/{agent_id}")
    async def receive(agent_id: str, request: Request) -> dict[str, bool]:
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/a2a+json":
            raise HTTPException(415, "Expected application/a2a+json")
        delivery_id = request.headers.get(A2A_DELIVERY_ID_HEADER, "")
        if not _DELIVERY_ID.fullmatch(delivery_id):
            raise HTTPException(400, "Invalid delivery ID")
        body = await request.body()
        if len(body) > 1024 * 1024:
            raise HTTPException(413, "Push body is too large")
        try:
            envelope = Parse(body.decode("utf-8"), a2a.StreamResponse())
        except (UnicodeError, ParseError) as error:
            raise HTTPException(400, "Malformed A2A push body") from error
        kind = envelope.WhichOneof("payload")
        if kind == "task":
            task_id, context_id = envelope.task.id, envelope.task.context_id
        elif kind == "message":
            task_id, context_id = envelope.message.task_id, envelope.message.context_id
        elif kind == "status_update":
            task_id, context_id = envelope.status_update.task_id, envelope.status_update.context_id
        elif kind == "artifact_update":
            task_id, context_id = envelope.artifact_update.task_id, envelope.artifact_update.context_id
        else:
            raise HTTPException(400, "Push has no task identity")
        if not task_id or not context_id or client.registry.get(agent_id) is None:
            raise HTTPException(400, "Unknown agent or task identity")
        route = await asyncio.to_thread(repository.route, agent_id, task_id)
        if route is None:
            raise HTTPException(503, "Task binding is not ready")
        thread, _ = route
        if thread["agent_id"] != agent_id or thread["context_id"] != context_id:
            raise HTTPException(409, "Push context does not match thread")
        thread_id = str(thread["thread_id"])
        try:
            receipt = await asyncio.to_thread(
                repository.receipt, agent_id, delivery_id, task_id, thread_id, context_id, kind
            )
        except ReceiptConflict as error:
            raise HTTPException(409, str(error)) from error
        if receipt["published_at"] is not None:
            return {"ok": True}
        claim = await asyncio.to_thread(repository.claim_task, agent_id, task_id)
        if claim is None:
            raise HTTPException(503, "Task observation is busy")
        try:
            current = await client.get_task(agent_id, task_id, history_length=0)
            if current.context_id != context_id:
                raise HTTPException(409, "Controller context does not match thread")
            state = a2a.TaskState.Name(current.status.state).removeprefix("TASK_STATE_")
            await asyncio.to_thread(repository.observe, agent_id, task_id, claim, delivery_id, state)
            await asyncio.to_thread(
                bus.publish,
                "app.a2a",
                A2A_TASK_UPDATED,
                A2ATaskUpdated(
                    delivery_id=delivery_id,
                    agent_id=agent_id,
                    task_id=task_id,
                    thread_id=thread_id,
                    context_id=context_id,
                    event_kind=kind,
                    task_state=state,
                ),
            )
            await asyncio.to_thread(repository.mark_published, agent_id, delivery_id)
            return {"ok": True}
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(503, "Task observation or event publication failed") from error
        finally:
            await asyncio.to_thread(repository.release_task, agent_id, task_id, claim)

    return app


def production_app() -> FastAPI:
    """Compose the private push receiver without exposing provider clients to surfaces."""
    config = load_config()
    if not config.cma_a2a_endpoint or not config.event_bus_name:
        raise RuntimeError("Private A2A endpoint and EventBridge bus are required")
    return create_app(
        EventSinkRepository(ThreadRepository(config)),
        A2AControllerClient(from_config(config)),
        EventBridgeEventBus(config.event_bus_name),
    )
