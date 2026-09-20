from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

from standardwebhooks import Webhook

from managed_agents_app.handlers import anthropic_webhook


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


def test_signed_completion_webhook_is_emitted(runtime, monkeypatch, config) -> None:
    secret = "whsec_" + base64.b64encode(b"test-webhook-secret").decode()
    configured = config.model_copy(update={"anthropic_webhook_signing_key": secret})
    emitted = []
    monkeypatch.setattr(anthropic_webhook, "load_config", lambda: configured)
    monkeypatch.setattr(runtime.events, "publish", lambda *args: emitted.append(args))
    body = json.dumps(
        {
            "id": "whe_test",
            "created_at": "2026-09-04T03:29:24Z",
            "type": "event",
            "data": {
                "id": "sesn_test",
                "organization_id": "org_test",
                "workspace_id": "wrkspc_test",
                "type": "session.status_idled",
            },
        },
        separators=(",", ":"),
    )
    event = signed_event(body, secret, "msg_test", datetime.now(UTC))
    result = anthropic_webhook.handler(event, None)
    assert result["statusCode"] == 200
    assert emitted[0][:2] == ("app.managed-agent", "ManagedAgentSessionChanged")
    assert emitted[0][2].session_id == "sesn_test"


def test_invalid_completion_webhook_is_rejected(runtime, monkeypatch, config) -> None:
    secret = "whsec_" + base64.b64encode(b"test-webhook-secret").decode()
    configured = config.model_copy(update={"anthropic_webhook_signing_key": secret})
    monkeypatch.setattr(anthropic_webhook, "load_config", lambda: configured)
    body = json.dumps({"type": "event"}, separators=(",", ":"))
    event = signed_event(body, secret, "msg_test", datetime.now(UTC))
    event["headers"]["webhook-signature"] = "v1,invalid"
    result = anthropic_webhook.handler(event, None)
    assert result["statusCode"] == 401
