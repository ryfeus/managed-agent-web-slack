from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from managed_agents_app.agui_bridge.app import create_app as create_agui_app
from managed_agents_app.cma_controller import webhook as anthropic_webhook
from managed_agents_app.handlers import slack_ingress, web_api
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
    app.include_router(create_agui_app(runtime).router)

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
        if path == "slack/events":
            result = await run_in_threadpool(slack_ingress.handle_request, runtime, event)
        elif path == "anthropic/webhook":
            result = await run_in_threadpool(anthropic_webhook.handle_request, runtime.config, event)
        else:
            result = await run_in_threadpool(web_api.handle_request, runtime, event)
        return Response(
            content=result["body"],
            status_code=result["statusCode"],
            headers=result.get("headers"),
            media_type="application/json",
        )

    return app


app = create_app()
