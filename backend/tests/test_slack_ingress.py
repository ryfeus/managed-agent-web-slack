from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import quote

from managed_agents_app.handlers import slack_ingress


def signed_event(body: str, secret: str, timestamp: str | None = None) -> dict:
    timestamp = timestamp or str(int(time.time()))
    base = f"v0:{timestamp}:{body}".encode()
    signature = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return {
        "httpMethod": "POST",
        "path": "/slack/events",
        "headers": {
            "content-type": "application/json",
            "x-slack-request-timestamp": timestamp,
            "x-slack-signature": signature,
        },
        "body": body,
    }


def test_signed_event_is_emitted_and_replay_window_is_enforced(runtime, monkeypatch, config) -> None:
    emitted = []
    monkeypatch.setattr(slack_ingress, "load_config", lambda: config)
    monkeypatch.setattr(runtime.events, "publish", lambda *args: emitted.append(args))
    body = json.dumps(
        {
            "type": "event_callback",
            "event_id": "Ev1",
            "team_id": "T1",
            "event": {
                "type": "app_mention",
                "user": "U1",
                "channel": "C1",
                "ts": "1",
                "text": "<@UBOT> hello",
            },
        },
        separators=(",", ":"),
    )
    accepted = slack_ingress.handler(signed_event(body, "slack-secret"), None)
    assert accepted["statusCode"] == 200 and len(emitted) == 1
    stale = slack_ingress.handler(signed_event(body, "slack-secret", "1"), None)
    assert stale["statusCode"] == 401


def test_signed_user_token_bot_event_needs_exact_configured_pair(runtime) -> None:
    runtime.config = runtime.config.model_copy(
        update={"slack_user_bot_id": "B_USER", "slack_user_app_id": "A_USER"}
    )
    payload = {
        "type": "event_callback",
        "event_id": "EvUserBot",
        "team_id": "T1",
        "event": {
            "type": "app_mention",
            "user": "U1",
            "channel": "C1",
            "ts": "1",
            "text": "<@UBOT> hello",
            "bot_id": "B_USER",
            "app_id": "A_USER",
        },
    }
    event = signed_event(json.dumps(payload), "slack-secret")
    accepted = slack_ingress.handle_request(runtime, event)
    assert accepted["statusCode"] == 200
    runtime.events.publish.assert_called_once()
    assert runtime.events.publish.call_args.args[1] == "SlackMessageReceived"
    runtime.events.publish.reset_mock()
    payload["event"]["app_id"] = "A_OTHER"
    ignored = slack_ingress.handle_request(runtime, signed_event(json.dumps(payload), "slack-secret"))
    assert ignored["statusCode"] == 200
    runtime.events.publish.assert_not_called()


def test_malformed_and_signed_interaction_payloads(runtime, monkeypatch, config) -> None:
    emitted = []
    monkeypatch.setattr(slack_ingress, "load_config", lambda: config)
    monkeypatch.setattr(runtime.events, "publish", lambda *args: emitted.append(args))
    invalid = slack_ingress.handler(signed_event("{", "slack-secret"), None)
    assert invalid["statusCode"] == 400
    payload = {
        "type": "message_action",
        "callback_id": "investigate",
        "team": {"id": "T1"},
        "user": {"id": "U1"},
        "channel": {"id": "C1"},
        "message": {"ts": "2", "thread_ts": "1"},
    }
    body = "payload=" + quote(json.dumps(payload, separators=(",", ":")))
    event = signed_event(body, "slack-secret")
    event["path"] = "/slack/interactions"
    event["headers"]["content-type"] = "application/x-www-form-urlencoded"
    result = slack_ingress.handler(event, None)
    assert result["statusCode"] == 200 and emitted[-1][1] == "SlackShortcutReceived"
