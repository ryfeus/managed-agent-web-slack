"""Private loopback-only hint queue and future-event SSE fixture."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from managed_agents_app.cma_controller.preview import PreviewHint
from managed_agents_app.slack.preview import SlackPreviewHint


class LocalPreviewTrigger:
    def __init__(self) -> None:
        self.hints: deque[dict[str, Any]] = deque()
        self.streams = 0
        self.connected = 0

    def start(self, hint: PreviewHint | SlackPreviewHint) -> None:
        self.hints.append(hint.wire())


def preview_router(runtime: Any) -> APIRouter:
    router = APIRouter()
    harness = runtime.local_a2a
    trigger = harness.preview_trigger

    @router.get("/_test/slack-preview-hints")
    def slack_hints() -> list[dict[str, Any]]:
        result = list(harness.slack_preview_trigger.hints)
        harness.slack_preview_trigger.hints.clear()
        return result

    @router.post("/_test/slack-preview-control")
    async def slack_control(request: Request) -> dict[str, bool]:
        body = await request.json()
        runtime.slack_preview_trigger = harness.slack_preview_trigger if body["enabled"] else None
        return {"ok": True}

    @router.post("/_test/slack-preview-append")
    async def slack_append(request: Request) -> dict[str, Any]:
        try:
            return dict(runtime.slack.call("chat.appendStream", await request.json()))
        except Exception as error:
            code = str(error)
            return {
                "ok": False,
                "error": code
                if code in {"rate_limited", "message_not_in_streaming_state"}
                else "unavailable",
            }

    @router.get("/_test/preview-hints")
    def hints() -> list[dict[str, Any]]:
        result = []
        while trigger.hints:
            result.append(trigger.hints.popleft())
        return result

    @router.post("/_test/preview-control")
    async def control(request: Request) -> dict[str, bool]:
        body = await request.json()
        harness.composition.scheduler.preview_trigger = trigger if body["enabled"] else None
        return {"ok": True}

    @router.get("/_test/preview-readers")
    def readers() -> dict[str, int]:
        return {"started": trigger.streams, "connected": trigger.connected}

    @router.get("/v1/sessions/{session_id}/events/stream")
    async def stream_events(session_id: str, request: Request) -> StreamingResponse:
        assert request.query_params.getlist("event_deltas[]") == ["agent.message"]
        # Like CMA, only events emitted after this connection opens are visible.
        offset = len(harness.provider.events[session_id])
        trigger.streams += 1
        trigger.connected += 1

        async def produce() -> AsyncIterator[str]:
            nonlocal offset
            try:
                while not await request.is_disconnected():
                    events = harness.provider.events.get(session_id)
                    if events is None:
                        return
                    for event in events[offset:]:
                        offset += 1
                        yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                    await asyncio.sleep(0.025)
            finally:
                trigger.connected -= 1

        return StreamingResponse(produce(), media_type="text/event-stream")

    return router
