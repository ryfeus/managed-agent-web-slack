from __future__ import annotations

from typing import Any

from anthropic import Anthropic

from managed_agents_app.cma_controller.runtime_repository import ControllerRuntimeRepository
from managed_agents_app.cma_controller.trigger import SchedulerTrigger
from managed_agents_app.cma_controller.trigger_sqs import production_trigger
from managed_agents_app.config import AppConfig, load_config
from managed_agents_app.http import headers, raw_body, response
from managed_agents_app.logging import log
from managed_agents_app.managed_agent.client import as_dict

SUPPORTED = {"session.status_idled", "session.status_terminated", "session.budget_reached"}
CONTROLLER_SUPPORTED = SUPPORTED | {"session.status_rescheduled"}


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    config = load_config()
    if config.app_env == "e2e":
        raise RuntimeError("E2E runtime is only available through local_api")
    repository = ControllerRuntimeRepository(config) if config.cma_scheduler_queue_url else None
    trigger = production_trigger(config.cma_scheduler_queue_url) if config.cma_scheduler_queue_url else None
    return handle_request(config, event, repository, trigger)


def handle_request(
    config: AppConfig,
    event: dict[str, Any],
    controller_repository: ControllerRuntimeRepository | None = None,
    controller_trigger: SchedulerTrigger | None = None,
) -> dict[str, Any]:
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
    if webhook_type not in CONTROLLER_SUPPORTED or not isinstance(session_id, str):
        return response(200, {"ok": True, "ignored": True})
    if controller_repository is None or controller_trigger is None:
        return response(503, {"error": "scheduler unavailable"})
    try:
        controller_context = controller_repository.context_for_session(session_id)
        if controller_context is not None:
            controller_trigger.schedule(str(controller_context["context_id"]), "cma-webhook")
            log(
                "info",
                "cma_webhook_wakeup",
                webhook_type=webhook_type,
                context_id=controller_context["context_id"],
            )
            return response(200, {"ok": True})
    except Exception as error:
        log("error", "cma_webhook_wakeup_failed", error_type=type(error).__name__)
        return response(503, {"error": "scheduler unavailable"})
    return response(200, {"ok": True, "ignored": True})
