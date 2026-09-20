from __future__ import annotations

from typing import Any

from anthropic import Anthropic

from managed_agents_app.config import load_config
from managed_agents_app.domain import MANAGED_AGENT_SESSION_CHANGED, ManagedAgentSessionChanged
from managed_agents_app.handlers.http import headers, raw_body, response
from managed_agents_app.logging import log
from managed_agents_app.managed_agent.client import as_dict
from managed_agents_app.runtime import Runtime, get_runtime

SUPPORTED = {"session.status_idled", "session.status_terminated", "session.budget_reached"}


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    config = load_config()
    if config.app_env == "e2e":
        raise RuntimeError("E2E runtime is only available through local_api")
    return handle_request(get_runtime(config), event)


def handle_request(runtime: Runtime, event: dict[str, Any]) -> dict[str, Any]:
    config = runtime.config
    if not config.anthropic_webhook_signing_key:
        return response(503, {"error": "Webhook is not configured"})
    try:
        raw = Anthropic(api_key="webhook-verification-only").beta.webhooks.unwrap(
            raw_body(event), headers=headers(event), key=config.anthropic_webhook_signing_key
        )
        value = as_dict(raw)
    except Exception as error:
        log("warning", "anthropic_webhook_rejected", error_type=type(error).__name__)
        return response(401, {"error": "invalid signature"})
    data = value.get("data") or {}
    webhook_type = data.get("type")
    session_id = data.get("id")
    if webhook_type not in SUPPORTED or not isinstance(session_id, str):
        return response(200, {"ok": True, "ignored": True})
    detail = ManagedAgentSessionChanged(
        webhook_event_id=str(value["id"]), webhook_type=webhook_type, session_id=session_id
    )
    runtime.events.publish("app.managed-agent", MANAGED_AGENT_SESSION_CHANGED, detail)
    log(
        "info",
        "anthropic_webhook_accepted",
        webhook_event_id=detail.webhook_event_id,
        webhook_type=webhook_type,
        session_id=session_id,
    )
    return response(200, {"ok": True})
