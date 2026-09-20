from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from managed_agents_app.handlers import anthropic_webhook, slack_ingress, web_api, web_stream
from managed_agents_app.runtime import Runtime, get_runtime


def create_app(runtime: Runtime | None = None) -> FastAPI:
    runtime = runtime or get_runtime()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:3000", "http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["content-type"],
    )
    if runtime.config.app_env == "e2e":
        from managed_agents_app.testing.api import router

        app.include_router(router(runtime))

    @app.get("/api/sessions/{session_id}/stream", response_model=None)
    def stream(session_id: str, request: Request) -> Any:
        return web_stream.stream_session(session_id, request, runtime)

    @app.api_route("/{path:path}", methods=["GET", "POST", "DELETE", "OPTIONS"])
    async def buffered_api(path: str, request: Request) -> Response:
        body = await request.body()
        event: dict[str, Any] = {
            "httpMethod": request.method,
            "path": f"/{path}",
            "headers": dict(request.headers),
            "body": body.decode(),
            "isBase64Encoded": False,
        }
        handler = (
            slack_ingress.handle_request
            if path == "slack/events"
            else anthropic_webhook.handle_request
            if path == "anthropic/webhook"
            else web_api.handle_request
        )
        result = await run_in_threadpool(handler, runtime, event)
        if runtime.config.app_env == "e2e":
            from typing import cast

            from managed_agents_app.testing.fake_agent import FakeManagedAgent

            cast(FakeManagedAgent, runtime.agent).flush_notifications()
        return Response(
            content=result["body"],
            status_code=result["statusCode"],
            headers=result.get("headers"),
            media_type="application/json",
        )

    return app


app = create_app()
