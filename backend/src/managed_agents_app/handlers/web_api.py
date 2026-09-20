from __future__ import annotations

import json
import os
import re
import uuid
from contextlib import suppress
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from managed_agents_app.auth import (
    clear_session_cookie,
    create_session_cookie,
    principal_from_cookie,
    verify_access_token,
)
from managed_agents_app.config import load_config
from managed_agents_app.db import Database, IngressClaim
from managed_agents_app.handlers.http import headers, response
from managed_agents_app.logging import log
from managed_agents_app.managed_agent.events import tool_is_pending
from managed_agents_app.ports.agent import AgentGateway
from managed_agents_app.runtime import Runtime, get_runtime


class CreateSessionBody(BaseModel):
    clientRequestId: uuid.UUID
    title: str | None = Field(default=None, min_length=1, max_length=60)


class MessageBody(BaseModel):
    clientRequestId: uuid.UUID
    text: str = Field(min_length=1, max_length=20_000)


class ConfirmationBody(BaseModel):
    toolUseId: str = Field(min_length=1)
    approved: bool
    reason: str | None = Field(default=None, max_length=1000)


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    config = load_config()
    if config.app_env == "e2e":
        raise RuntimeError("E2E runtime is only available through local_api")
    return handle_request(get_runtime(config), event)


def handle_request(runtime: Runtime, event: dict[str, Any]) -> dict[str, Any]:
    request_headers = headers(event)
    origin = request_headers.get("origin", "")
    cors = {
        "access-control-allow-origin": origin if origin.startswith("http://localhost:") else origin,
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

    auth = _authenticate(request_headers.get("cookie"), runtime)
    if path == "/api/auth/session" and method == "GET":
        return response(200 if auth else 401, {"authenticated": bool(auth)}, extra_headers=cors)
    if not auth:
        return response(401, {"error": "unauthorized"}, extra_headers=cors)
    principal_id, db, managed = auth

    if path == "/api/sessions" and method == "GET":
        sessions = []
        for row in db.list_sessions(principal_id):
            try:
                sessions.append(managed.retrieve_session(str(row["session_id"])).model_dump())
            except Exception:
                continue
        return response(200, {"data": sessions}, extra_headers=cors)

    if path == "/api/sessions" and method == "POST":
        create_body = _validate(CreateSessionBody, event, cors)
        if isinstance(create_body, dict):
            return create_body
        request_id = str(create_body.clientRequestId)
        claim = db.claim_ingress("web-session", request_id)
        if claim == IngressClaim.BUSY:
            return response(202, {"status": "pending"}, extra_headers=cors)
        session = managed.find_by_creation_request_id(request_id)
        try:
            if not session:
                session = managed.create_session(
                    principal_id=principal_id,
                    surface="web",
                    creation_request_id=request_id,
                    title=create_body.title,
                )
            db.register_session(session.id, principal_id, config.agent_id, config.environment_id, "web")
            db.complete_ingress("web-session", request_id, session.id)
            log("info", "web_session_created", principal_id=principal_id, session_id=session.id)
            return response(201, session.model_dump(), extra_headers=cors)
        except Exception as error:
            db.fail_ingress("web-session", request_id, str(error))
            raise

    match = re.fullmatch(r"/api/sessions/([^/]+)(?:/(archive|events|messages|confirm|interrupt))?", path)
    if not match:
        return response(404, {"error": "not found"}, extra_headers=cors)
    session_id, action = match.groups()
    if not db.owns_session(principal_id, session_id):
        return response(404, {"error": "session not found"}, extra_headers=cors)
    if not action and method == "GET":
        return response(200, managed.retrieve_session(session_id).model_dump(), extra_headers=cors)
    if action == "archive" and method == "POST":
        managed.archive(session_id)
        db.mark_session_archived(principal_id, session_id)
        return response(200, {"ok": True}, extra_headers=cors)
    if action == "events" and method == "GET":
        return response(200, {"data": managed.list_events(session_id)}, extra_headers=cors)
    if action == "messages" and method == "POST":
        message_body = _validate(MessageBody, event, cors)
        if isinstance(message_body, dict):
            return message_body
        request_id = str(message_body.clientRequestId)
        if db.claim_ingress("web-message", request_id) != IngressClaim.ACQUIRED:
            return response(202, {"ok": True, "duplicate": True}, extra_headers=cors)
        try:
            text = message_body.text.strip()
            managed_event_id = managed.send_message(session_id, text)
            db.complete_ingress("web-message", request_id, session_id, managed_event_id)
            with suppress(Exception):
                managed.update_title(session_id, text)
            return response(202, {"ok": True, "managedEventId": managed_event_id}, extra_headers=cors)
        except Exception as error:
            db.fail_ingress("web-message", request_id, str(error))
            raise
    if action == "confirm" and method == "POST":
        confirmation_body = _validate(ConfirmationBody, event, cors)
        if isinstance(confirmation_body, dict):
            return confirmation_body
        if not tool_is_pending(managed.list_events(session_id), confirmation_body.toolUseId):
            return response(409, {"error": "tool approval is no longer pending"}, extra_headers=cors)
        managed.confirm_tool(
            session_id,
            confirmation_body.toolUseId,
            confirmation_body.approved,
            confirmation_body.reason,
        )
        return response(202, {"ok": True}, extra_headers=cors)
    if action == "interrupt" and method == "POST":
        managed.interrupt(session_id)
        return response(202, {"ok": True}, extra_headers=cors)
    return response(404, {"error": "not found"}, extra_headers=cors)


def _authenticate(cookie: str | None, runtime: Runtime) -> tuple[str, Database, AgentGateway] | None:
    config = runtime.config
    principal_id = principal_from_cookie(cookie, config.web_cookie_secret)
    if not principal_id:
        return None
    return principal_id, runtime.db, runtime.agent


def _json(event: dict[str, Any]) -> Any:
    try:
        return json.loads(event.get("body") or "null")
    except json.JSONDecodeError:
        return None


def _validate[T: BaseModel](
    model: type[T], event: dict[str, Any], cors: dict[str, str]
) -> T | dict[str, Any]:
    try:
        return model.model_validate(_json(event))
    except ValidationError as error:
        return response(400, {"error": "invalid request", "issues": error.errors()}, extra_headers=cors)
