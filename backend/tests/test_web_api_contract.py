from __future__ import annotations

import json
from pathlib import Path

from managed_agents_app.db import IngressClaim
from managed_agents_app.handlers import web_api
from managed_agents_app.managed_agent import SessionSummary

CONTRACTS = json.loads((Path(__file__).parent / "fixtures" / "contracts.json").read_text())


class FakeDatabase:
    def list_sessions(self, _principal_id):
        return [{"session_id": "sesn_1"}]

    def owns_session(self, _principal_id, session_id):
        return session_id == "sesn_1"

    def claim_ingress(self, _surface, _request_id):
        return IngressClaim.ACQUIRED

    def complete_ingress(self, *_args):
        return None

    def fail_ingress(self, *_args):
        return None

    def register_session(self, *_args):
        return None

    def mark_session_archived(self, *_args):
        return None


class FakeManaged:
    def __init__(self) -> None:
        self.session = SessionSummary.model_validate(CONTRACTS["session"])
        self.confirmations = []

    def retrieve_session(self, _session_id):
        return self.session

    def find_by_creation_request_id(self, _request_id):
        return self.session

    def list_events(self, _session_id):
        return [CONTRACTS["managed_event"]]

    def send_message(self, *_args):
        return "evt_input"

    def confirm_tool(self, *_args):
        self.confirmations.append(_args)

    def interrupt(self, *_args):
        return None

    def archive(self, *_args):
        return None

    def update_title(self, *_args):
        return None


def request(method: str, path: str, body: object | None = None) -> dict:
    return {
        "httpMethod": method,
        "path": path,
        "headers": {"cookie": "test"},
        "body": json.dumps(body) if body is not None else None,
    }


def decoded(result: dict) -> dict:
    return json.loads(result["body"])


def test_public_contracts(monkeypatch, config) -> None:
    monkeypatch.setattr(web_api, "load_config", lambda: config)
    health = web_api.handler(request("GET", "/health"), None)
    assert health["statusCode"] == CONTRACTS["health"]["statusCode"]
    assert decoded(health) == CONTRACTS["health"]["body"]

    monkeypatch.setattr(web_api, "_authenticate", lambda *_: None)
    denied = web_api.handler(request("GET", "/api/sessions"), None)
    assert denied["statusCode"] == CONTRACTS["unauthorized"]["statusCode"]
    assert decoded(denied) == CONTRACTS["unauthorized"]["body"]


def test_session_rest_contracts(monkeypatch, config) -> None:
    fake_db = FakeDatabase()
    fake_managed = FakeManaged()
    monkeypatch.setattr(web_api, "load_config", lambda: config)
    monkeypatch.setattr(
        web_api,
        "_authenticate",
        lambda *_: ("principal-1", fake_db, fake_managed),
    )

    listed = web_api.handler(request("GET", "/api/sessions"), None)
    assert listed["statusCode"] == 200
    assert decoded(listed) == {"data": [CONTRACTS["session"]]}

    fetched = web_api.handler(request("GET", "/api/sessions/sesn_1"), None)
    assert fetched["statusCode"] == 200 and decoded(fetched) == CONTRACTS["session"]

    events = web_api.handler(request("GET", "/api/sessions/sesn_1/events"), None)
    assert decoded(events) == {"data": [CONTRACTS["managed_event"]]}

    message = web_api.handler(
        request(
            "POST",
            "/api/sessions/sesn_1/messages",
            {"clientRequestId": "5ae39557-b341-4dff-818b-751857e95ae3", "text": "hello"},
        ),
        None,
    )
    assert message["statusCode"] == 202
    assert decoded(message) == {"ok": True, "managedEventId": "evt_input"}

    fake_managed.list_events = lambda _session_id: [
        {"id": "tool_1", "type": "agent.tool_use", "name": "bash", "input": {}},
        {
            "id": "idle_1",
            "type": "session.status_idle",
            "stop_reason": {"type": "requires_action", "event_ids": ["tool_1"]},
        },
    ]
    confirm = web_api.handler(
        request(
            "POST",
            "/api/sessions/sesn_1/confirm",
            {"toolUseId": "tool_1", "approved": True},
        ),
        None,
    )
    assert confirm["statusCode"] == 202 and decoded(confirm) == {"ok": True}
    assert fake_managed.confirmations[-1] == ("sesn_1", "tool_1", True, None)

    stale = web_api.handler(
        request(
            "POST",
            "/api/sessions/sesn_1/confirm",
            {"toolUseId": "tool_missing", "approved": True},
        ),
        None,
    )
    assert stale["statusCode"] == 409
    assert decoded(stale) == {"error": "tool approval is no longer pending"}
    assert web_api.handler(request("POST", "/api/sessions/sesn_1/interrupt", {}), None)["statusCode"] == 202
    assert web_api.handler(request("POST", "/api/sessions/sesn_1/archive", {}), None)["statusCode"] == 200
