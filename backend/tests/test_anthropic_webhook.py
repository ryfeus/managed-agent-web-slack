from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

from standardwebhooks import Webhook

from managed_agents_app.cma_controller import webhook as anthropic_webhook


def signed_event(body: str, secret: str, message_id: str, timestamp: datetime) -> dict:
    return {
        "httpMethod": "POST",
        "path": "/anthropic/webhook",
        "headers": {
            "content-type": "application/json",
            "webhook-id": message_id,
            "webhook-timestamp": str(int(timestamp.timestamp())),
            "webhook-signature": Webhook(secret).sign(message_id, timestamp, body),
        },
        "body": body,
    }


def test_signed_unknown_session_is_ignored(config) -> None:
    secret = "whsec_" + base64.b64encode(b"test-webhook-secret").decode()
    configured = config.model_copy(update={"anthropic_webhook_signing_key": secret})
    event = signed_event(_completion_body("sesn_unknown"), secret, "msg_test", datetime.now(UTC))
    repository = MagicMock()
    repository.context_for_session.return_value = None
    trigger = MagicMock()
    result = anthropic_webhook.handle_request(configured, event, repository, trigger)
    assert result["statusCode"] == 200
    assert json.loads(result["body"])["ignored"] is True
    trigger.schedule.assert_not_called()


def _completion_body(session_id: str) -> str:
    return json.dumps(
        {
            "id": "whe_test",
            "created_at": "2026-09-04T03:29:24Z",
            "type": "event",
            "data": {
                "id": session_id,
                "organization_id": "org_test",
                "workspace_id": "wrkspc_test",
                "type": "session.status_idled",
            },
        },
        separators=(",", ":"),
    )


def test_invalid_completion_webhook_is_rejected(runtime, monkeypatch, config) -> None:
    secret = "whsec_" + base64.b64encode(b"test-webhook-secret").decode()
    configured = config.model_copy(update={"anthropic_webhook_signing_key": secret})
    monkeypatch.setattr(anthropic_webhook, "load_config", lambda: configured)
    body = json.dumps({"type": "event"}, separators=(",", ":"))
    event = signed_event(body, secret, "msg_test", datetime.now(UTC))
    event["headers"]["webhook-signature"] = "v1,invalid"
    result = anthropic_webhook.handler(event, None)
    assert result["statusCode"] == 401


def test_controller_owned_webhook_wakes_scheduler_and_ignores_unknown(runtime, config) -> None:
    secret = "whsec_" + base64.b64encode(b"test-webhook-secret").decode()
    runtime.config = config.model_copy(update={"anthropic_webhook_signing_key": secret})
    body = json.dumps(
        {
            "id": "whe_controller",
            "created_at": "2026-09-04T03:29:24Z",
            "type": "event",
            "data": {
                "id": "sesn_controller",
                "organization_id": "org_test",
                "workspace_id": "wrkspc_test",
                "type": "session.status_idled",
            },
        },
        separators=(",", ":"),
    )
    event = signed_event(body, secret, "msg_controller", datetime.now(UTC))
    repository = MagicMock()
    repository.context_for_session.return_value = {"context_id": "context_1"}
    trigger = MagicMock()
    result = anthropic_webhook.handle_request(runtime.config, event, repository, trigger)
    assert result["statusCode"] == 200
    trigger.schedule.assert_called_once_with("context_1", "cma-webhook")

    repository.context_for_session.return_value = None
    result = anthropic_webhook.handle_request(runtime.config, event, repository, trigger)
    assert result["statusCode"] == 200
    assert json.loads(result["body"])["ignored"] is True
    trigger.schedule.assert_called_once_with("context_1", "cma-webhook")

    repository.context_for_session.return_value = {"context_id": "context_1"}
    trigger.schedule.side_effect = RuntimeError("SQS unavailable")
    result = anthropic_webhook.handle_request(runtime.config, event, repository, trigger)
    assert result["statusCode"] == 503
