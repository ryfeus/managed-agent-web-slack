"""Private SQS push worker and scheduled maintenance entrypoint."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from managed_agents_app.cma_controller.composition import build_controller
from managed_agents_app.config import load_config

logger = logging.getLogger(__name__)


async def _handle(event: dict[str, Any]) -> dict[str, Any]:
    config = load_config()
    if not config.cma_push_queue_url or not config.cma_pending_input_bucket:
        raise RuntimeError("CMA push queue and pending-input bucket must be configured")
    composition = build_controller(config)
    dispatcher = composition.push_dispatcher
    assert dispatcher is not None
    try:
        if event.get("maintenance") == "a2a-push":
            return {"scheduled": await dispatcher.maintenance()}
        if "maintenance" in event or not isinstance(event.get("Records"), list):
            raise ValueError("Unknown push worker invocation")
        failures: list[dict[str, str]] = []
        for record in event.get("Records", []):
            try:
                payload = json.loads(record["body"])
                if (
                    payload.get("version") != 1
                    or not isinstance(payload.get("contextId"), str)
                    or not payload["contextId"]
                    or not isinstance(payload.get("reason"), str)
                    or not payload["reason"]
                ):
                    raise ValueError("Invalid push wakeup")
                await dispatcher.run_context(payload["contextId"])
            except Exception:
                logger.exception("cma_push_worker_record_failed")
                failures.append({"itemIdentifier": str(record["messageId"])})
        return {"batchItemFailures": failures}
    finally:
        await composition.close()


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    return asyncio.run(_handle(event))
