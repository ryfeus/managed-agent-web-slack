from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs

from managed_agents_app.config import load_config
from managed_agents_app.handlers.http import headers, raw_body, response
from managed_agents_app.logging import log
from managed_agents_app.runtime import Runtime, get_runtime
from managed_agents_app.slack import (
    normalize_event,
    normalize_interaction,
    verify_slack_signature,
)
from managed_agents_app.slack.normalize import ModalOpenRequest


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    config = load_config()
    if config.app_env == "e2e":
        raise RuntimeError("E2E runtime is only available through local_api")
    return handle_request(get_runtime(config), event)


def handle_request(runtime: Runtime, event: dict[str, Any]) -> dict[str, Any]:
    config = runtime.config
    if not config.slack_signing_secret:
        return response(503, {"error": "Slack is not configured"})
    body = raw_body(event)
    request_headers = headers(event)
    if not verify_slack_signature(
        config.slack_signing_secret,
        request_headers.get("x-slack-request-timestamp"),
        request_headers.get("x-slack-signature"),
        body,
    ):
        return response(401, {"error": "invalid signature"})

    try:
        if "application/x-www-form-urlencoded" in request_headers.get("content-type", ""):
            parsed = parse_qs(body, keep_blank_values=True)
            payload = json.loads(parsed.get("payload", ["{}"])[0])
            first_action = (payload.get("actions") or [{}])[0]
            log(
                "info",
                "slack_interaction_received",
                interaction_type=payload.get("type"),
                action_id=first_action.get("action_id") if isinstance(first_action, dict) else None,
                callback_id=payload.get("callback_id") or (payload.get("view") or {}).get("callback_id"),
            )
            normalized = normalize_interaction(payload, body)
            if isinstance(normalized, ModalOpenRequest):
                if not config.slack_bot_token:
                    return response(503, {"error": "Slack bot token is not configured"})
                runtime.slack.open_denial_modal(normalized.trigger_id, normalized.metadata)
                return response(200, {"ok": True})
        else:
            payload = json.loads(body)
            if payload.get("type") == "url_verification" and isinstance(payload.get("challenge"), str):
                return response(200, {"challenge": payload["challenge"]})
            normalized = normalize_event(payload)
    except json.JSONDecodeError, TypeError, ValueError:
        return response(400, {"error": "invalid payload"})

    if not normalized:
        log("info", "slack_event_ignored", payload_type=payload.get("type"))
        return response(200, {"ok": True, "ignored": True})
    detail_type, detail = normalized
    runtime.events.publish("app.slack", detail_type, detail)
    log("info", "slack_event_accepted", detail_type=detail_type)
    return response(200, {"ok": True})
