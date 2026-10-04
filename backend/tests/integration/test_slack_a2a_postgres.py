"""Signed Slack ingress through SQS, application service, controller, push sink and Slack."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from uuid import uuid4

import pytest
from psycopg import sql

from managed_agents_app.config import load_config
from managed_agents_app.db.connection import connect
from managed_agents_app.handlers.slack_ingress import handle_request
from managed_agents_app.testing.api import TABLES
from managed_agents_app.testing.factory import create_runtime, validate_local_database


@pytest.fixture
def local():
    config = load_config().model_copy(
        update={
            "slack_streaming_enabled": True,
            "slack_task_cards_enabled": True,
            "slack_agent_view_enabled": True,
            "slack_receipt_reaction_enabled": True,
            "slack_source_links_enabled": True,
            "slack_tool_approvals_enabled": True,
            "slack_feedback_enabled": True,
            "slack_shortcuts_enabled": True,
            "slack_unfurls_enabled": True,
            "slack_bound_thread_replies": True,
            "public_app_url": "http://localhost:3000",
        }
    )
    validate_local_database(config)
    runtime = create_runtime(config)
    with connect(runtime.config) as conn:
        conn.execute(sql.SQL("TRUNCATE {} CASCADE").format(sql.SQL(", ").join(map(sql.Identifier, TABLES))))
    runtime.db.ensure_principal(config.dev_principal_id)
    runtime.db.map_external_identity(config.dev_principal_id, "slack", "T001", "U001")
    runtime.db.ensure_principal("00000000-0000-4000-8000-000000000002")
    runtime.db.map_external_identity("00000000-0000-4000-8000-000000000002", "slack", "T001", "U002")
    return runtime


def signed(local, payload: dict, *, interaction: bool = False) -> None:
    from urllib.parse import urlencode

    body = urlencode({"payload": json.dumps(payload)}) if interaction else json.dumps(payload)
    # The signature verifier checks freshness; use current time for the request.
    import time

    timestamp = str(int(time.time()))
    signature = hmac.new(
        (local.config.slack_signing_secret or "").encode(),
        f"v0:{timestamp}:{body}".encode(),
        hashlib.sha256,
    ).hexdigest()
    response = handle_request(
        local,
        {
            "body": body,
            "headers": {
                "content-type": "application/x-www-form-urlencoded" if interaction else "application/json",
                "x-slack-request-timestamp": timestamp,
                "x-slack-signature": f"v0={signature}",
            },
        },
    )
    assert response["statusCode"] == 200


def mention(
    text: str,
    event_id: str = "Ev1",
    *,
    user: str = "U001",
    ts: str = "100.000001",
    thread_ts: str | None = None,
    event_type: str = "app_mention",
) -> dict:
    event = {"type": event_type, "user": user, "channel": "C001", "ts": ts, "text": text}
    if thread_ts:
        event["thread_ts"] = thread_ts
    return {"type": "event_callback", "team_id": "T001", "event_id": event_id, "event": event}


def drain(local) -> None:
    for _ in range(8):
        local.events.drain()
        local.events.agent_input_queue.drain()
        asyncio.run(local.local_a2a.drain())
        state = local.events.snapshot()
        queue = local.events.agent_input_queue.snapshot()
        if not state["pending"] and not any(item["state"] == "visible" for item in queue["messages"]):
            break
    assert not [item for item in local.events.snapshot()["history"] if item["status"] == "failed"]


def rows(local, table: str) -> list[dict]:
    with connect(local.config) as conn:
        return list(conn.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))).fetchall())


def test_signed_turn_and_duplicate_use_one_task_and_one_slack_card(local):
    event = mention("hello")
    signed(local, event)
    signed(local, event)
    drain(local)
    assert len(rows(local, "surface_ingress_events")) == 1
    assert len(rows(local, "agent_threads")) == 1
    assert len(rows(local, "thread_surface_bindings")) == 1
    assert len(rows(local, "agent_tasks")) == 1
    assert len(rows(local, "cma_contexts")) == 1
    assert local.local_a2a.provider.create_calls == 1
    assert len(local.slack.streams) == 1
    assert local.slack.streams["1000.000001"]["text"] == "Done"
    assert len(local.slack.reactions) == 1
    assert "Original message" in json.dumps(local.slack.streams["1000.000001"]["chunks"])


def test_bound_reply_reuses_thread_and_rejects_another_owner(local):
    signed(local, mention("first"))
    drain(local)
    signed(local, mention("second", "Ev2", ts="100.000002", thread_ts="100.000001", event_type="message"))
    drain(local)
    assert len(rows(local, "agent_threads")) == 1
    assert len(rows(local, "agent_tasks")) == 2
    assert len(rows(local, "cma_contexts")) == 1
    signed(
        local,
        mention(
            "forbidden", "Ev3", user="U002", ts="100.000003", thread_ts="100.000001", event_type="message"
        ),
    )
    drain(local)
    assert len(rows(local, "agent_tasks")) == 2
    assert any("not authorized" in str(item["text"]) for item in local.slack.messages.values())


def test_approval_reason_and_competing_response(local):
    local.local_a2a.provider.script(
        "fetch",
        [
            {
                "id": "private-tool",
                "type": "agent.tool_use",
                "name": "web_fetch",
                "input": {"url": "https://example.test"},
            },
            {
                "type": "session.status_idle",
                "stop_reason": {"type": "requires_action", "event_ids": ["private-tool"]},
            },
        ],
    )
    signed(local, mention("fetch"))
    drain(local)
    approval = next(
        item for item in local.slack.messages.values() if "agent_tool_allow" in json.dumps(item["blocks"])
    )
    value = approval["blocks"][1]["elements"][0]["value"]
    ids = json.loads(value)
    assert ids["taskId"] and ids["requestId"]
    assert "private-tool" not in value
    deny = {
        "type": "block_actions",
        "team": {"id": "T001"},
        "user": {"id": "U001"},
        "channel": {"id": "C001"},
        "trigger_id": "trigger",
        "message": {"ts": approval["ts"], "thread_ts": "100.000001"},
        "actions": [{"action_id": "agent_tool_deny_with_reason", "value": value}],
    }
    signed(local, deny, interaction=True)
    modal = local.slack.modals[0]["view"]
    signed(
        local,
        {
            "type": "view_submission",
            "team": {"id": "T001"},
            "user": {"id": "U001"},
            "view": {
                "id": "modal-1",
                "callback_id": "agent_tool_deny_reason",
                "private_metadata": modal["private_metadata"],
                "state": {"values": {"reason": {"value": {"value": "Unsafe URL"}}}},
            },
        },
        interaction=True,
    )
    drain(local)
    session_id = next(iter(local.local_a2a.provider.sessions))
    confirmations = [
        e for e in local.local_a2a.provider.events[session_id] if e["type"] == "user.tool_confirmation"
    ]
    assert len(confirmations) == 1
    assert confirmations[0]["deny_message"] == "Unsafe URL"
    assert rows(local, "cma_input_requests")[0]["decision_reason_object_key"]
    # A later opposite decision cannot replay provider confirmation.
    allow = {
        **deny,
        "actions": [{"action_id": "agent_tool_allow", "value": value, "action_ts": str(uuid4())}],
    }
    signed(local, allow, interaction=True)
    drain(local)
    assert local.local_a2a.provider.confirm_calls == 1
    assert any("already resolved" in str(item["text"]) for item in local.slack.messages.values())


@pytest.mark.parametrize("streaming", [True, False])
def test_approval_resume_posts_one_visible_answer(local, streaming):
    local.config = local.config.model_copy(
        update={"slack_streaming_enabled": streaming, "slack_task_cards_enabled": streaming}
    )
    local.local_a2a.provider.script(
        "fetch",
        [
            {
                "id": "private-tool",
                "type": "agent.tool_use",
                "name": "web_fetch",
                "input": {"url": "https://example.test"},
            },
            {
                "type": "session.status_idle",
                "stop_reason": {"type": "requires_action", "event_ids": ["private-tool"]},
            },
        ],
    )
    signed(local, mention("fetch"))
    drain(local)
    approval = next(
        item for item in local.slack.messages.values() if "agent_tool_allow" in json.dumps(item.get("blocks"))
    )
    assert rows(local, "cma_tasks")[0]["a2a_state"] == "INPUT_REQUIRED"
    assert not any(item["text"] == "Done" for item in local.slack.messages.values())
    signed(
        local,
        {
            "type": "block_actions",
            "team": {"id": "T001"},
            "user": {"id": "U001"},
            "channel": {"id": "C001"},
            "trigger_id": "trigger",
            "message": {"ts": approval["ts"], "thread_ts": "100.000001"},
            "actions": [
                {"action_id": "agent_tool_allow", "value": approval["blocks"][1]["elements"][0]["value"]}
            ],
        },
        interaction=True,
    )
    drain(local)
    assert local.local_a2a.provider.confirm_calls == 1
    assert rows(local, "cma_tasks")[0]["a2a_state"] == "COMPLETED"
    answers = [item for item in local.slack.messages.values() if item["text"] == "Done"]
    assert len(answers) == 1
    answer = answers[0]
    assert answer["thread_ts"] == "100.000001"
    assert "".join(b["text"]["text"] for b in answer["blocks"] if b["type"] == "section") == "Done"
    assert any(b["type"] == "context_actions" for b in answer["blocks"])
    assert any(b["type"] == "context" for b in answer["blocks"])
    assert "agent_tool_allow" not in json.dumps(local.slack.messages[approval["ts"]])
    thread = rows(local, "agent_threads")[0]
    current = rows(local, "agent_tasks")[0]
    local.events.publish(
        "app.a2a",
        "A2ATaskUpdated",
        {
            "deliveryId": str(uuid4()),
            "agentId": "cma",
            "threadId": str(thread["thread_id"]),
            "taskId": current["task_id"],
            "contextId": thread["context_id"],
            "eventKind": "task",
            "taskState": "COMPLETED",
        },
    )
    drain(local)
    assert [item for item in local.slack.messages.values() if item["text"] == "Done"] == answers


def test_task_update_projects_to_every_slack_binding(local):
    signed(local, mention("hello"))
    drain(local)
    thread_id = str(rows(local, "agent_threads")[0]["thread_id"])
    task = rows(local, "agent_tasks")[0]
    local.threads.bind_surface(thread_id, "slack", "T001", "C002:200.000001")
    local.events.publish(
        "app.a2a",
        "A2ATaskUpdated",
        {
            "deliveryId": str(uuid4()),
            "agentId": "cma",
            "threadId": thread_id,
            "taskId": task["task_id"],
            "contextId": rows(local, "agent_threads")[0]["context_id"],
            "eventKind": "task",
            "taskState": "COMPLETED",
        },
    )
    drain(local)
    assert len(rows(local, "slack_task_projections")) == 2
    assert len(rows(local, "slack_projection_items")) == 2
    assert len([m for m in local.slack.messages.values() if m["channel"] == "C002"]) == 1


def test_lost_application_response_retries_same_controller_task(local):
    local.faults.arm("after_agent_send", "EvLost", "raise")
    signed(local, mention("lost", "EvLost"))
    local.events.drain()
    local.events.agent_input_queue.drain()
    asyncio.run(local.local_a2a.drain())
    assert len(rows(local, "agent_tasks")) == 1
    assert rows(local, "surface_ingress_events")[0]["status"] == "failed"
    local.events.agent_input_queue.advance_time(360)
    drain(local)
    assert len(rows(local, "agent_tasks")) == 1
    assert local.local_a2a.provider.create_calls == 1
    assert len(rows(local, "slack_projection_items")) == 1
    assert len(local.slack.messages) == 1


def test_live_push_race_retries_current_task_without_duplicate(local):
    signed(local, mention("hello"))
    drain(local)
    thread = rows(local, "agent_threads")[0]
    task_id = rows(local, "agent_tasks")[0]["task_id"]
    binding = local.threads.bind_surface(str(thread["thread_id"]), "slack", "T001", "C002:200.000001")
    binding_id = str(binding["binding_id"])
    local.slack_a2a.ensure_projection(binding_id, task_id)
    claim = local.slack_a2a.claim_task_stream(binding_id, task_id)
    assert claim.status == "acquired" and claim.token
    local.events.publish(
        "app.a2a",
        "A2ATaskUpdated",
        {
            "deliveryId": str(uuid4()),
            "agentId": "cma",
            "threadId": str(thread["thread_id"]),
            "taskId": task_id,
            "contextId": thread["context_id"],
            "eventKind": "task",
            "taskState": "COMPLETED",
        },
    )
    local.events.drain()
    failure = next(item for item in local.events.snapshot()["history"] if item["status"] == "failed")
    assert "live projection" in failure["error"]
    local.slack_a2a.finish_stream(binding_id, task_id, claim.token, fallback=True, state=None)
    local.events.retry(failure["id"])
    drain(local)
    assert len([m for m in local.slack.messages.values() if m["channel"] == "C002"]) == 1
    assert (
        len([item for item in rows(local, "slack_projection_items") if str(item["binding_id"]) == binding_id])
        == 1
    )


def test_stop_cancels_active_task_only(local):
    local.config = local.config.model_copy(update={"slack_streaming_enabled": False})
    local.local_a2a.provider.automatic = False
    signed(local, mention("first"))
    drain(local)
    signed(local, mention("second", "Ev2", ts="100.000002", thread_ts="100.000001", event_type="message"))
    drain(local)
    tasks = rows(local, "cma_tasks")
    assert len(tasks) == 2
    signed(
        local,
        {
            "type": "event_callback",
            "team_id": "T001",
            "event_id": "EvStop",
            "event": {
                "type": "agent_session_stopped",
                "user": "U001",
                "channel": "C001",
                "thread_ts": "100.000001",
            },
        },
    )
    drain(local)
    tasks = rows(local, "cma_tasks")
    assert sum(item["a2a_state"] == "CANCELED" for item in tasks) == 1
    assert local.local_a2a.provider.interrupt_calls == 1
