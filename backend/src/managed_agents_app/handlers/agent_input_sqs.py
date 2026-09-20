from __future__ import annotations

import json
from typing import Any

from managed_agents_app.config import load_config
from managed_agents_app.handlers.agent_input import handle_domain_event
from managed_agents_app.logging import log
from managed_agents_app.runtime import Runtime, get_runtime


def handle_sqs_event(runtime: Runtime, event: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    records = event.get("Records")
    if not isinstance(records, list):
        raise ValueError("SQS event must contain a Records list")

    failures: list[dict[str, str]] = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("SQS record must be an object")
        message_id = record.get("messageId")
        if not isinstance(message_id, str) or not message_id:
            # Lambda partial-batch responses cannot name a record without its message ID.
            raise ValueError("SQS record is missing a usable messageId")
        attributes = record.get("attributes")
        receive_count = attributes.get("ApproximateReceiveCount") if isinstance(attributes, dict) else None
        detail_type: str | None = None
        try:
            body = record.get("body")
            if not isinstance(body, str):
                raise ValueError("SQS record body must be a JSON string")
            domain_event = json.loads(body)
            if not isinstance(domain_event, dict):
                raise ValueError("SQS record body must decode to an event object")
            raw_detail_type = domain_event.get("detail-type")
            detail_type = raw_detail_type if isinstance(raw_detail_type, str) else None
            handle_domain_event(runtime, domain_event)
            log(
                "info",
                "sqs_agent_input_processed",
                sqs_message_id=message_id,
                approximate_receive_count=receive_count,
                detail_type=detail_type,
                outcome="completed",
            )
        except Exception as error:
            failures.append({"itemIdentifier": message_id})
            log(
                "error",
                "sqs_agent_input_failed",
                sqs_message_id=message_id,
                approximate_receive_count=receive_count,
                detail_type=detail_type,
                outcome="retry",
                error=type(error).__name__,
            )
    return {"batchItemFailures": failures}


def handler(event: dict[str, Any], _context: Any) -> dict[str, list[dict[str, str]]]:
    config = load_config()
    if config.app_env == "e2e":
        raise RuntimeError("E2E runtime is only available through local_api")
    return handle_sqs_event(get_runtime(config), event)
