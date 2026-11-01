"""Private ASGI receiver for standard A2A push envelopes."""

from __future__ import annotations

import asyncio
import hashlib
import re

from a2a.types import a2a_pb2 as a2a
from fastapi import FastAPI, HTTPException, Request
from google.protobuf.json_format import Parse, ParseError  # type: ignore[import-untyped]

from managed_agents_app.agents.registry import AgentRegistry, from_config
from managed_agents_app.config import load_config
from managed_agents_app.db.thread_repository import ThreadRepository
from managed_agents_app.domain import A2A_TASK_UPDATED, A2ATaskUpdated
from managed_agents_app.events import EventBridgeEventBus
from managed_agents_app.ports.events import EventBus
from managed_agents_app.protocols.controller_profile import A2A_DELIVERY_ID_HEADER

_DELIVERY_ID = re.compile(r"^[0-9a-f]{64}$")
_MAX_BODY_BYTES = 1024 * 1024


def create_app(threads: ThreadRepository, registry: AgentRegistry, bus: EventBus) -> FastAPI:
    app = FastAPI()

    @app.post("/internal/a2a/events/{agent_id}")
    async def receive(agent_id: str, request: Request) -> dict[str, bool]:
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/a2a+json":
            raise HTTPException(415, "Expected application/a2a+json")
        delivery_id = request.headers.get(A2A_DELIVERY_ID_HEADER, "")
        if delivery_id and not _DELIVERY_ID.fullmatch(delivery_id):
            raise HTTPException(400, "Invalid delivery ID")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > _MAX_BODY_BYTES:
                raise HTTPException(413, "Push body is too large")
            body.extend(chunk)
        try:
            envelope = Parse(body.decode("utf-8"), a2a.StreamResponse())
        except (UnicodeError, ParseError) as error:
            raise HTTPException(400, "Malformed A2A push body") from error
        kind = envelope.WhichOneof("payload")
        state = None
        if kind == "task":
            task_id, context_id = envelope.task.id, envelope.task.context_id
            state = envelope.task.status.state
        elif kind == "message":
            task_id, context_id = envelope.message.task_id, envelope.message.context_id
        elif kind == "status_update":
            task_id, context_id = envelope.status_update.task_id, envelope.status_update.context_id
            state = envelope.status_update.status.state
        elif kind == "artifact_update":
            task_id, context_id = envelope.artifact_update.task_id, envelope.artifact_update.context_id
        else:
            raise HTTPException(400, "Push has no task identity")
        if (
            not all(value.strip() and len(value) <= 512 for value in (task_id, context_id))
            or registry.get(agent_id) is None
        ):
            raise HTTPException(400, "Unknown agent or task identity")
        if state is not None and state not in a2a.TaskState.values():
            raise HTTPException(400, "Unknown Task state")
        binding = await asyncio.to_thread(threads.get_task_binding, agent_id, task_id)
        thread = await asyncio.to_thread(threads.get_thread, str(binding["thread_id"])) if binding else None
        if thread is None:
            raise HTTPException(503, "Task binding is not ready")
        if thread["agent_id"] != agent_id or thread["context_id"] != context_id:
            raise HTTPException(409, "Push context does not match thread")
        thread_id = str(thread["thread_id"])
        # Trust comes from private ingress, never from this correlation identifier.
        delivery_id = delivery_id or hashlib.sha256(body).hexdigest()
        try:
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
                    task_state=a2a.TaskState.Name(state).removeprefix("TASK_STATE_") if state else None,
                ),
            )
            return {"ok": True}
        except Exception as error:
            raise HTTPException(503, "Event publication failed") from error

    return app


def production_app() -> FastAPI:
    """Compose the private push receiver without exposing provider clients to surfaces."""
    config = load_config()
    if not config.cma_a2a_endpoint or not config.event_bus_name:
        raise RuntimeError("Private A2A endpoint and EventBridge bus are required")
    return create_app(
        ThreadRepository(config),
        from_config(config),
        EventBridgeEventBus(config.event_bus_name),
    )
