from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from managed_agents_app.auth import principal_from_cookie
from managed_agents_app.config import load_config
from managed_agents_app.logging import log
from managed_agents_app.runtime import Runtime, get_runtime

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_STREAM_END = object()
_HEARTBEAT_SECONDS = 15.0


def _next_event(iterator: Iterator[Any]) -> object:
    try:
        return next(iterator)
    except StopIteration:
        return _STREAM_END


@app.get("/")
def health() -> dict[str, bool]:
    return {"ok": True}


def stream_session(session_id: str, request: Request, runtime: Runtime) -> StreamingResponse | JSONResponse:
    config = runtime.config
    principal_id = principal_from_cookie(request.headers.get("cookie"), config.web_cookie_secret)
    if not principal_id or not runtime.db.owns_session(principal_id, session_id):
        return JSONResponse({"error": "session not found"}, status_code=404)

    async def events() -> AsyncIterator[str]:
        yield ": connected\n\n"
        managed = runtime.agent
        try:
            iterator = iter(managed.stream_events(session_id))
            next_task: asyncio.Task[object] | None = None
            while not await request.is_disconnected():
                try:
                    if next_task is None:
                        next_task = asyncio.create_task(asyncio.to_thread(_next_event, iterator))
                    next_item = await asyncio.wait_for(asyncio.shield(next_task), timeout=_HEARTBEAT_SECONDS)
                    next_task = None
                except TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                except Exception:
                    raise
                if next_item is _STREAM_END:
                    break
                event = next_item
                if not isinstance(event, dict):
                    continue
                event_id = f"id: {event['id']}\n" if event.get("id") else ""
                yield f"{event_id}event: managed-agent\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"
                if event.get("type") in {"session.status_idle", "session.status_terminated"}:
                    break
        except Exception as error:
            log(
                "error",
                "managed_agent_stream_error",
                principal_id=principal_id,
                session_id=session_id,
                error=str(error),
            )
            yield f"event: error\ndata: {json.dumps({'message': 'stream failed'})}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@app.get("/api/sessions/{session_id}/stream", response_model=None)
def production_stream(session_id: str, request: Request) -> StreamingResponse | JSONResponse:
    config = load_config()
    if config.app_env == "e2e":
        raise RuntimeError("E2E runtime is only available through local_api")
    return stream_session(session_id, request, get_runtime(config))
