"""SQS Lambda entrypoint for one-step CMA context reconciliation."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from managed_agents_app.cma_controller.composition import build_controller
from managed_agents_app.config import load_config


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    config = load_config()
    if not config.cma_pending_input_bucket or not config.cma_scheduler_queue_url:
        raise RuntimeError("CMA scheduler storage and queue must be configured")
    scheduler = build_controller(config, with_dispatcher=False).scheduler
    if event.get("maintenance") == "pending-input-gc":
        deleted, stale = scheduler.sweep_orphans()
        return {"deleted": deleted, "stale": stale}

    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        try:
            payload = json.loads(record["body"])
            if payload["version"] != 1 or not isinstance(payload["contextId"], str):
                raise ValueError("Invalid scheduler payload")
            asyncio.run(scheduler.run_once(payload["contextId"]))
        except Exception:
            failures.append({"itemIdentifier": str(record["messageId"])})
    return {"batchItemFailures": failures}
