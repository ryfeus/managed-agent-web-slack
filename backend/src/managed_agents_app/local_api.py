from __future__ import annotations

from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from managed_agents_app.cma_controller import webhook as anthropic_webhook
from managed_agents_app.config import local_cors_origins
from managed_agents_app.handlers import slack_ingress
from managed_agents_app.runtime import Runtime, get_runtime
from managed_agents_app.testing.factory import LocalA2AHarness


def create_app(runtime: Runtime | None = None) -> FastAPI:
    runtime = runtime or get_runtime()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=local_cors_origins(runtime.config),
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["content-type"],
    )
    if runtime.config.app_env == "e2e":
        from managed_agents_app.testing.api import router
        from managed_agents_app.testing.preview import preview_router

        app.include_router(router(runtime))
        app.include_router(preview_router(runtime))
        app.mount("/a2a", cast(LocalA2AHarness, runtime.local_a2a).controller)

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
        if path in {"slack/events", "slack/interactions"}:
            result = await run_in_threadpool(slack_ingress.handle_request, runtime, event)
        elif path == "anthropic/webhook":
            result = await run_in_threadpool(anthropic_webhook.handle_request, runtime.config, event)
        else:
            return Response(status_code=404)
        return Response(
            content=result["body"],
            status_code=result["statusCode"],
            headers=result.get("headers"),
            media_type="application/json",
        )

    return app


app = create_app()
