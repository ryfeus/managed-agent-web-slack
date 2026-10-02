from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from managed_agents_app.handlers import web_api

THREAD_ID = "03cb1122-146c-4dab-8393-1573ee38e195"
REQUEST_ID = "cc7866ba-5859-4cf0-9748-7c603339544d"


class FakeThreads:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.by_request: dict[str, str] = {}

    def create_thread_idempotent(self, principal: str, agent: str, request: str, title: str | None):
        if request in self.by_request:
            return self.rows[self.by_request[request]]
        now = datetime(2026, 9, 27, tzinfo=UTC)
        row = {
            "thread_id": THREAD_ID,
            "principal_id": principal,
            "agent_id": agent,
            "title": title,
            "created_at": now,
            "updated_at": now,
            "archived_at": None,
            "last_task_id": None,
            "last_task_state": None,
        }
        self.rows[THREAD_ID] = row
        self.by_request[request] = THREAD_ID
        return row

    def list_threads(self, principal: str):
        return [
            row
            for row in self.rows.values()
            if row["principal_id"] == principal and row["archived_at"] is None
        ]

    def get_owned_thread(self, principal: str, thread_id: str):
        row = self.rows.get(thread_id)
        return row if row and row["principal_id"] == principal else None

    def archive_thread(self, principal: str, thread_id: str):
        row = self.get_owned_thread(principal, thread_id)
        if row:
            row["archived_at"] = datetime(2026, 9, 27, tzinfo=UTC)
        return row

    def restore_thread(self, principal: str, thread_id: str):
        row = self.get_owned_thread(principal, thread_id)
        if row:
            row["archived_at"] = None
        return row


def request(method: str, path: str, body: object | None = None) -> dict:
    return {
        "httpMethod": method,
        "path": path,
        "headers": {"cookie": "test"},
        "body": json.dumps(body) if body is not None else None,
    }


def decoded(result: dict) -> dict:
    return json.loads(result["body"])


def test_thread_metadata_api_and_removed_session_routes(monkeypatch, config) -> None:
    threads = FakeThreads()
    runtime = SimpleNamespace(config=config, threads=threads)
    monkeypatch.setattr(web_api, "principal_from_cookie", lambda *_: "principal-1")

    body = {"clientRequestId": REQUEST_ID}
    created = web_api.handle_request(runtime, request("POST", "/api/threads", body))
    assert created["statusCode"] == 201
    assert decoded(created)["id"] == THREAD_ID
    assert decoded(created)["agentId"] == "cma"
    retried = web_api.handle_request(runtime, request("POST", "/api/threads", body))
    assert decoded(retried)["id"] == THREAD_ID
    assert len(threads.rows) == 1
    listed = web_api.handle_request(runtime, request("GET", "/api/threads"))
    assert [item["id"] for item in decoded(listed)["data"]] == [THREAD_ID]
    assert web_api.handle_request(runtime, request("GET", f"/api/threads/{THREAD_ID}"))["statusCode"] == 200
    archived = web_api.handle_request(runtime, request("POST", f"/api/threads/{THREAD_ID}/archive"))
    assert decoded(archived)["archivedAt"] is not None
    assert decoded(web_api.handle_request(runtime, request("GET", "/api/threads")))["data"] == []
    restored = web_api.handle_request(runtime, request("POST", f"/api/threads/{THREAD_ID}/restore"))
    assert decoded(restored)["archivedAt"] is None
    assert web_api.handle_request(runtime, request("GET", "/api/sessions"))["statusCode"] == 404


def test_thread_ownership_is_not_disclosed(monkeypatch, config) -> None:
    threads = FakeThreads()
    threads.create_thread_idempotent("principal-1", config.agent_id, REQUEST_ID, None)
    runtime = SimpleNamespace(config=config, threads=threads)
    monkeypatch.setattr(web_api, "principal_from_cookie", lambda *_: "principal-2")
    result = web_api.handle_request(runtime, request("GET", f"/api/threads/{THREAD_ID}"))
    assert result["statusCode"] == 404
    monkeypatch.setattr(web_api, "principal_from_cookie", lambda *_: None)
    assert web_api.handle_request(runtime, request("GET", "/api/threads"))["statusCode"] == 401
