"""Public cookie-authenticated AG-UI bridge; controller traffic stays private."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from a2a.types import a2a_pb2 as a2a
from ag_ui.core import RunAgentInput, RunErrorEvent, RunStartedEvent, UserMessage
from ag_ui.encoder import EventEncoder
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from managed_agents_app.agent_control_plane.composition import application_control_plane
from managed_agents_app.agent_control_plane.service import ThreadAgentService
from managed_agents_app.agui_bridge.history import canonical_history
from managed_agents_app.agui_bridge.mapper import interrupts_for
from managed_agents_app.agui_bridge.service import stream_task
from managed_agents_app.auth import principal_from_cookie
from managed_agents_app.config import load_config
from managed_agents_app.protocols.human_input import ClarificationResponse, ToolApprovalResponse
from managed_agents_app.runtime import Runtime, get_runtime


def _thread_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as error:
        raise HTTPException(404, "thread not found") from error


async def _authorize(runtime: Runtime, request: Request, thread_id: str) -> str:
    principal = principal_from_cookie(request.headers.get("cookie"), runtime.config.web_cookie_secret)
    if principal is None:
        raise HTTPException(401, "unauthorized")
    valid_id = _thread_id(thread_id)
    owned = await asyncio.to_thread(runtime.threads.owns_thread, principal, valid_id)
    if not owned:
        raise HTTPException(404, "thread not found")
    return valid_id


def _new_text(input_value: RunAgentInput) -> str:
    if not input_value.messages or not isinstance(input_value.messages[-1], UserMessage):
        raise HTTPException(400, "A new user message is required")
    content = input_value.messages[-1].content
    if not isinstance(content, str) or not content.strip() or len(content) > 20_000:
        raise HTTPException(400, "Only nonempty text input is supported")
    return content.strip()


async def _respond(
    service: ThreadAgentService, thread_id: str, run_id: str, task: a2a.Task, entries: list[Any]
) -> a2a.Task:
    current = await service.get_task(thread_id, task.id, human_input=True)
    pending = {request.id: request for request in interrupts_for(current)}
    supplied = {entry.interrupt_id: entry for entry in entries}
    if not pending or set(supplied) != set(pending) or len(entries) != len(pending):
        raise HTTPException(409, "Pending interrupts changed; reload this conversation")
    for interrupt_id, item in pending.items():
        entry = supplied[interrupt_id]
        if entry.status != "resolved" or not isinstance(entry.payload, dict):
            raise HTTPException(400, "Every interrupt needs an explicit response")
        payload = entry.payload
        try:
            if item.reason == "tool_approval":
                response: ToolApprovalResponse | ClarificationResponse = ToolApprovalResponse(
                    request_id=interrupt_id,
                    decision=payload.get("decision"),
                    reason=payload.get("reason"),
                )
            else:
                response = ClarificationResponse(request_id=interrupt_id, answer=payload.get("answer"))
        except ValidationError as error:
            raise HTTPException(400, "Invalid interrupt response") from error
        await service.continue_human_input(
            thread_id,
            task.id,
            f"web:agui:{run_id}:interrupt:{interrupt_id}",
            response.model_dump(by_alias=True, exclude_none=True),
        )
    return await service.get_task(thread_id, task.id, human_input=True)


def create_app(runtime: Runtime | None = None) -> FastAPI:
    runtime = runtime or get_runtime()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:3000", "http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["content-type"],
    )

    @app.post("/api/agui")
    async def run(request: Request) -> StreamingResponse:
        principal = principal_from_cookie(request.headers.get("cookie"), runtime.config.web_cookie_secret)
        if principal is None:
            raise HTTPException(401, "unauthorized")
        try:
            input_value = RunAgentInput.model_validate(await request.json())
        except (ValidationError, ValueError) as error:
            raise HTTPException(400, "Invalid AG-UI run input") from error
        thread_id = await _authorize(runtime, request, input_value.thread_id)
        if not input_value.run_id or len(input_value.run_id) > 128:
            raise HTTPException(400, "Invalid run ID")
        text = _new_text(input_value) if not input_value.resume else None

        async def events() -> Any:
            started = False
            try:
                async with application_control_plane(runtime) as service:
                    if input_value.resume:
                        active = await service.get_active_task(thread_id)
                        if active is None:
                            raise HTTPException(409, "No active task to resume")
                        task = await _respond(
                            service, thread_id, input_value.run_id, active, input_value.resume
                        )
                        snapshot = True
                    else:
                        assert text is not None
                        task = await service.send_turn(thread_id, f"web:agui:{input_value.run_id}", text)
                        await asyncio.to_thread(runtime.threads.update_title_if_empty, thread_id, text[:60])
                        snapshot = False
                    async for encoded in stream_task(
                        service,
                        thread_id,
                        input_value.run_id,
                        task.id,
                        snapshot=snapshot,
                        await_resume_progress=bool(input_value.resume),
                        accept=request.headers.get("accept"),
                    ):
                        started = True
                        yield encoded
            except asyncio.CancelledError:
                raise
            except Exception:
                encoder = EventEncoder(request.headers.get("accept") or "text/event-stream")
                if not started:
                    yield encoder.encode(RunStartedEvent(thread_id=thread_id, run_id=input_value.run_id))
                yield encoder.encode(
                    RunErrorEvent(message="The task could not be started. Reload to reconnect.")
                )

        return StreamingResponse(
            events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/api/agui/threads/{thread_id}/history")
    async def history(request: Request, thread_id: str) -> dict[str, Any]:
        valid_id = await _authorize(runtime, request, thread_id)
        async with application_control_plane(runtime) as service:
            return await canonical_history(service, valid_id)

    @app.get("/api/agui/threads/{thread_id}/resume")
    async def resume(request: Request, thread_id: str) -> StreamingResponse:
        valid_id = await _authorize(runtime, request, thread_id)
        async with application_control_plane(runtime) as service:
            active = await service.get_active_task(valid_id)
            if active is None:
                raise HTTPException(409, "No active task")
            thread = await asyncio.to_thread(runtime.threads.get_thread, valid_id)
            if thread is None:
                raise HTTPException(404, "thread not found")
            binding = await asyncio.to_thread(
                runtime.threads.get_task_binding,
                str(thread["agent_id"]),
                active.id,
            )
            original = str(binding["client_message_id"]) if binding else ""
            run_id = (
                original.removeprefix("web:agui:")
                if original.startswith("web:agui:")
                else f"reconnect:{active.id}"
            )

        async def events() -> Any:
            async with application_control_plane(runtime) as stream_service:
                async for encoded in stream_task(
                    stream_service,
                    valid_id,
                    run_id,
                    active.id,
                    snapshot=True,
                    accept=request.headers.get("accept"),
                ):
                    yield encoded

        return StreamingResponse(
            events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    @app.post("/api/agui/threads/{thread_id}/cancel")
    async def cancel(request: Request, thread_id: str) -> dict[str, Any]:
        valid_id = await _authorize(runtime, request, thread_id)
        async with application_control_plane(runtime) as service:
            task = await service.cancel_active_task(valid_id)
        return {"ok": True, "taskId": task.id if task else None}

    return app


def production_app() -> FastAPI:
    config = load_config()
    if config.app_env == "e2e":
        raise RuntimeError("E2E runtime is only available through local_api")
    return create_app(get_runtime(config))
