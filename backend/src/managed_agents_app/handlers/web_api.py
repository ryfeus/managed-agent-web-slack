"""Cookie-authenticated Web identity and application thread metadata API."""

from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from managed_agents_app.agents.registry import DEFAULT_APPLICATION_AGENT_ID
from managed_agents_app.auth import (
    clear_session_cookie,
    create_session_cookie,
    principal_from_cookie,
    verify_access_token,
)
from managed_agents_app.config import load_config
from managed_agents_app.db.thread_repository import BindingConflict, ThreadRepository
from managed_agents_app.http import headers, raw_body, response
from managed_agents_app.logging import log
from managed_agents_app.runtime import Runtime, get_runtime


class CreateThreadBody(BaseModel):
    clientRequestId: uuid.UUID
    title: str | None = Field(default=None, min_length=1, max_length=60)


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    config = load_config()
    if config.app_env == "e2e":
        raise RuntimeError("E2E runtime is only available through local_api")
    return handle_request(get_runtime(config), event)


def handle_request(runtime: Runtime, event: dict[str, Any]) -> dict[str, Any]:
    request_headers = headers(event)
    origin = request_headers.get("origin", "")
    cors = {
        "access-control-allow-origin": origin,
        "access-control-allow-credentials": "true",
        "access-control-allow-headers": "content-type",
        "access-control-allow-methods": "GET,POST,DELETE,OPTIONS",
    }
    try:
        if event.get("httpMethod") == "OPTIONS":
            return response(204, {}, extra_headers=cors)
        return _route(runtime, event, request_headers, cors)
    except Exception as error:
        log("error", "web_api_error", error=str(error))
        return response(500, {"error": "internal server error"}, extra_headers=cors)


def _thread_json(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["thread_id"]),
        "agentId": str(row["agent_id"]),
        "title": row["title"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "archivedAt": row["archived_at"],
        "lastTaskId": row["last_task_id"],
        "lastTaskState": row["last_task_state"],
    }


def _route(
    runtime: Runtime, event: dict[str, Any], request_headers: dict[str, str], cors: dict[str, str]
) -> dict[str, Any]:
    method = str(event.get("httpMethod", "GET"))
    path = str(event.get("path", "/"))
    config = runtime.config

    if method == "GET" and path == "/health":
        return response(200, {"ok": True}, extra_headers=cors)
    if path == "/api/auth/session" and method == "POST":
        body = _json(event)
        actual = body.get("accessToken") if isinstance(body, dict) else None
        if not isinstance(actual, str) or not verify_access_token(actual, config.web_access_token):
            return response(401, {"error": "invalid access token"}, extra_headers=cors)
        secure = request_headers.get("x-forwarded-proto") != "http" and os.getenv("APP_ENV") == "production"
        return response(
            200,
            {"ok": True},
            extra_headers=cors,
            cookies=[create_session_cookie(config.dev_principal_id, config.web_cookie_secret, secure=secure)],
        )
    if path == "/api/auth/session" and method == "DELETE":
        secure = request_headers.get("x-forwarded-proto") != "http" and os.getenv("APP_ENV") == "production"
        return response(200, {"ok": True}, extra_headers=cors, cookies=[clear_session_cookie(secure=secure)])

    principal_id = principal_from_cookie(request_headers.get("cookie"), config.web_cookie_secret)
    if path == "/api/auth/session" and method == "GET":
        return response(
            200 if principal_id else 401, {"authenticated": bool(principal_id)}, extra_headers=cors
        )
    if not principal_id:
        return response(401, {"error": "unauthorized"}, extra_headers=cors)

    threads: ThreadRepository = runtime.threads
    if path == "/api/threads" and method == "GET":
        return response(
            200,
            {"data": [_thread_json(row) for row in threads.list_threads(principal_id)]},
            extra_headers=cors,
        )
    if path == "/api/threads" and method == "POST":
        try:
            body = CreateThreadBody.model_validate(_json(event))
        except ValidationError:
            return response(400, {"error": "invalid request"}, extra_headers=cors)
        try:
            row = threads.create_thread_idempotent(
                principal_id, DEFAULT_APPLICATION_AGENT_ID, str(body.clientRequestId), body.title
            )
        except BindingConflict:
            return response(409, {"error": "creation request conflict"}, extra_headers=cors)
        return response(201, _thread_json(row), extra_headers=cors)

    match = re.fullmatch(r"/api/threads/([0-9a-fA-F-]+)(?:/(archive|restore))?", path)
    if match is None:
        return response(404, {"error": "not found"}, extra_headers=cors)
    thread_id, action = match.groups()
    try:
        thread_id = str(uuid.UUID(thread_id))
    except ValueError:
        return response(404, {"error": "not found"}, extra_headers=cors)
    owned = threads.get_owned_thread(principal_id, thread_id)
    if owned is None:
        return response(404, {"error": "thread not found"}, extra_headers=cors)
    if action is None and method == "GET":
        return response(200, _thread_json(owned), extra_headers=cors)
    if action == "archive" and method == "POST":
        row = threads.archive_thread(principal_id, thread_id) or owned
        return response(200, _thread_json(row), extra_headers=cors)
    if action == "restore" and method == "POST":
        row = threads.restore_thread(principal_id, thread_id) or owned
        return response(200, _thread_json(row), extra_headers=cors)
    return response(404, {"error": "not found"}, extra_headers=cors)


def _json(event: dict[str, Any]) -> Any:
    try:
        return json.loads(raw_body(event) or "null")
    except json.JSONDecodeError:
        return None
