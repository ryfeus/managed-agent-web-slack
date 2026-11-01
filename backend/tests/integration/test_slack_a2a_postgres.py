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
def local(monkeypatch):
    config = load_config().model_copy(
        update={
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
    from managed_agents_app.agent_control_plane.service import ThreadAgentService

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Slack P0 must not subscribe")

    monkeypatch.setattr(ThreadAgentService, "subscribe_task", forbidden)
    yield runtime
    assert runtime.local_a2a.provider.stream_calls == 0, "CMA SSE is P1-only"


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
    assert next(iter(local.slack.streams.values()))["text"] == "Done"
    assert len(local.slack.reactions) == 1
    assert "Original message" in json.dumps(next(iter(local.slack.streams.values()))["chunks"])


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


def test_automatic_policy_and_stale_controls(local):
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
    assert not any(
        "agent_tool_allow" in json.dumps(item.get("blocks")) for item in local.slack.messages.values()
    )
    requests = rows(local, "cma_input_requests")
    assert requests[0]["decision"] == "allow" and requests[0]["status"] == "resolved"
    value = json.dumps({"taskId": requests[0]["task_id"], "requestId": requests[0]["request_id"]})
    # Stale controls cannot replace the machine-owned decision at either surface.
    for action in ("agent_tool_allow", "agent_tool_deny"):
        signed(
            local,
            {
                "type": "block_actions",
                "team": {"id": "T001"},
                "user": {"id": "U001"},
                "channel": {"id": "C001"},
                "message": {"ts": "100.000002", "thread_ts": "100.000001"},
                "actions": [{"action_id": action, "value": value, "action_ts": str(uuid4())}],
            },
            interaction=True,
        )
        drain(local)
    assert local.local_a2a.provider.confirm_calls == 1
    assert rows(local, "cma_input_requests")[0]["decision"] == "allow"


@pytest.mark.parametrize("stream_available", [True, False])
def test_policy_completion_posts_one_visible_answer(local, stream_available):
    if not stream_available:
        local.slack.fail_next("chat.startStream", "missing_scope")
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
    assert local.local_a2a.provider.confirm_calls == 1
    assert rows(local, "cma_tasks")[0]["a2a_state"] == "COMPLETED"
    answers = [item for item in local.slack.messages.values() if item["text"] == "Done"]
    assert len(answers) == 1
    answer = answers[0]
    assert answer["thread_ts"] == "100.000001"
    if stream_available:
        stream = local.slack.streams[answer["ts"]]
        assert stream["state"] == "completed" and stream["mode"] == "chunks"
        assert "".join(c["text"] for c in stream["chunks"] if c["type"] == "markdown_text") == "Done"
        assert "Original message" in json.dumps(stream["chunks"])
    else:
        assert "".join(b["text"]["text"] for b in answer["blocks"] if b["type"] == "section") == "Done"
        assert any(b["type"] == "context" for b in answer["blocks"])
    assert "agent_tool_allow" not in json.dumps(list(local.slack.messages.values()))
    import httpx
    from a2a.types import a2a_pb2 as a2a
    from google.protobuf.json_format import MessageToJson

    from managed_agents_app.a2a_event_sink.app import create_app as create_sink
    from managed_agents_app.agents.registry import AgentRegistration, AgentRegistry

    thread = rows(local, "agent_threads")[0]
    current = rows(local, "agent_tasks")[0]
    stale = a2a.StreamResponse(
        task=a2a.Task(
            id=current["task_id"],
            context_id=thread["context_id"],
            status=a2a.TaskStatus(state=a2a.TaskState.TASK_STATE_WORKING),
            history=[a2a.Message(message_id="preview", parts=[a2a.Part(text="Never render this preview")])],
        )
    )

    class LostAckBus:
        once = True

        def publish(self, source, detail_type, detail):
            local.events.publish(source, detail_type, detail)
            if self.once:
                self.once = False
                raise TimeoutError("EventBridge accepted but acknowledgment was lost")

    async def replay():
        sink = create_sink(
            local.threads, AgentRegistry([AgentRegistration("cma", "http://controller")]), LostAckBus()
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=sink), base_url="http://sink") as http:
            for expected in (503, 200, 200):
                response = await http.post(
                    "/internal/a2a/events/cma",
                    content=MessageToJson(stale),
                    headers={"Content-Type": "application/a2a+json", "X-A2A-Delivery-Id": "a" * 64},
                )
                assert response.status_code == expected

    asyncio.run(replay())
    drain(local)
    assert [item for item in local.slack.messages.values() if item["text"] == "Done"] == answers
    assert "Never render this preview" not in json.dumps(local.slack.messages)
    assert local.local_a2a.provider.confirm_calls == 1
    assert local.local_a2a.provider.create_calls == 1
    assert len(local.slack.streams) == int(stream_available)


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


def test_short_projection_claim_retries_current_task_without_duplicate(local):
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
    assert "projection is busy" in failure["error"]
    local.slack_a2a.finish_stream(binding_id, task_id, claim.token, fallback=True, state=None)
    local.events.retry(failure["id"])
    drain(local)
    assert len([m for m in local.slack.messages.values() if m["channel"] == "C002"]) == 1
    assert (
        len([item for item in rows(local, "slack_projection_items") if str(item["binding_id"]) == binding_id])
        == 1
    )


def test_stop_cancels_active_task_only(local):
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


@pytest.mark.parametrize("live", [False, True])
def test_clarification_settlement_retires_stream_before_resumed_projection(local, live):
    from a2a.types import a2a_pb2 as a2a

    from managed_agents_app.handlers.slack_projector import _project_task

    signed(local, mention("hello"))
    drain(local)
    projection = rows(local, "slack_task_projections")[0]
    binding_id, task_id = str(projection["binding_id"]), str(projection["task_id"])
    repo = local.slack_a2a
    repo.mark_projection(binding_id, task_id, "WORKING", "pending")
    claim = repo.claim_task_stream(binding_id, task_id)
    assert claim.token
    repo.update_stream_message(binding_id, task_id, claim.token, "1000.000001")
    if live:
        # An expired worker cannot retire its successor's stream.
        repo.finish_stream(binding_id, task_id, str(uuid4()), fallback=False, state="INPUT_REQUIRED")
        assert repo.get_projection(binding_id, task_id)["slack_message_ts"] == "1000.000001"
        repo.finish_stream(binding_id, task_id, claim.token, fallback=False, state="INPUT_REQUIRED")
    else:
        # Failed streams retain their pointer so a push can finish the same stream.
        repo.finish_stream(binding_id, task_id, claim.token, fallback=True, state="WORKING")
        assert repo.get_projection(binding_id, task_id)["slack_message_ts"] == "1000.000001"
        repo.mark_projection(binding_id, task_id, "INPUT_REQUIRED", "completed")
    assert repo.get_projection(binding_id, task_id)["slack_message_ts"] is None
    repo.mark_projection(binding_id, task_id, "WORKING", "pending")
    resumed = repo.get_projection(binding_id, task_id)
    assert resumed["slack_message_ts"] is None
    before = len(local.slack.messages)
    _project_task(
        local,
        local.slack,
        binding_id,
        task_id,
        "C001",
        "100.000001",
        a2a.Task(
            id=task_id,
            status=a2a.TaskStatus(state=a2a.TaskState.TASK_STATE_COMPLETED),
            history=[
                a2a.Message(
                    message_id="resumed-answer",
                    role=a2a.Role.ROLE_AGENT,
                    parts=[a2a.Part(text="Clarification answered")],
                )
            ],
        ),
        stream_ts=resumed["slack_message_ts"],
    )
    assert len(local.slack.messages) == before + 1
    assert repo.get_projection(binding_id, task_id)["status"] == "completed"


@pytest.mark.parametrize("push_first", [False, True])
def test_initial_setup_and_push_ordering_never_reopens_completed_card(local, push_first):
    from managed_agents_app.handlers.slack_projector import handle_domain_event

    local.local_a2a.provider.automatic = False
    signed(local, mention("controlled completion"))
    local.events.drain()
    local.events.agent_input_queue.drain()
    initial = next(e for e in local.events.queue if e["detail-type"] == "SlackTaskProjectionRequested")
    local.events.queue.remove(initial)
    asyncio.run(local.local_a2a.drain())
    if not push_first:
        handle_domain_event(local, initial)
        projection = rows(local, "slack_task_projections")[0]
        assert projection["status"] == "pending"
        assert projection["claim_id"] is None and projection["lease_expires_at"] is None
        assert next(iter(local.slack.streams.values()))["state"] != "completed"
    # Push may set up the card before the initial event, and is the only completion driver.
    drain(local)
    session_id = str(rows(local, "cma_contexts")[0]["cma_session_id"])
    local.local_a2a.provider.finish(session_id, "Canonical final")
    local.local_a2a.scheduler_trigger.schedule(
        str(rows(local, "cma_contexts")[0]["context_id"]), "test-wakeup"
    )
    drain(local)
    assert len(local.slack.streams) == 1
    stream = next(iter(local.slack.streams.values()))
    assert stream["state"] == "completed" and stream["text"] == "Canonical final"
    # Delayed or duplicate initial setup reads current state without starting another stream.
    handle_domain_event(local, initial)
    handle_domain_event(local, initial)
    assert len(local.slack.streams) == 1
    assert len(local.slack.messages) == 1
    projection = rows(local, "slack_task_projections")[0]
    assert projection["status"] == "completed" and projection["slack_message_ts"] is None
    assert projection["claim_id"] is None and projection["lease_expires_at"] is None
    assert local.local_a2a.provider.create_calls == 1


@pytest.mark.parametrize("ruling", ["allow", "deny"])
def test_signed_native_human_review_is_authorized_and_first_writer_wins(local, ruling, tmp_path):
    from pathlib import Path

    from managed_agents_app.policy.openappa import OpenAppaPolicyEngine

    policy = Path(__file__).resolve().parents[3] / "services/openappa/tests/human-review.toml"
    current_policy = tmp_path / "review.toml"
    current_policy.write_text(policy.read_text())
    local.local_a2a.composition.scheduler.policy_engine = OpenAppaPolicyEngine(
        local.config.openappa_binary_path, str(current_policy)
    )
    local.local_a2a.provider.script(
        "review",
        [
            {
                "id": "native-review",
                "type": "agent.tool_use",
                "name": "web_search",
                "input": {"query": "exact private query"},
            },
            {
                "type": "session.status_idle",
                "stop_reason": {"type": "requires_action", "event_ids": ["native-review"]},
            },
        ],
    )
    signed(local, mention("review"))
    drain(local)
    row = rows(local, "cma_input_requests")[0]
    assert rows(local, "cma_tasks")[0]["a2a_state"] == "INPUT_REQUIRED"
    assert local.local_a2a.provider.confirm_calls == 0
    message = next(
        m for m in local.slack.messages.values() if "agent_tool_allow" in json.dumps(m.get("blocks"))
    )
    assert "exact private query" in json.dumps(message["blocks"])
    assert "user-approval" in json.dumps(message["blocks"])
    assert "exact private query" not in str(row)

    def action(user, decision):
        return {
            "type": "block_actions",
            "team": {"id": "T001"},
            "user": {"id": user},
            "channel": {"id": "C001"},
            "message": {"ts": message["ts"], "thread_ts": "100.000001"},
            "actions": [
                {
                    "action_id": "agent_tool_" + decision,
                    "value": json.dumps({"taskId": row["task_id"], "requestId": row["request_id"]}),
                    "action_ts": str(uuid4()),
                }
            ],
        }

    # Same INPUT_REQUIRED state, new policy: push must retire the old controls
    # and expose the freshly offered request, without waiting for a user click.
    stale_action = action("U001", ruling)
    previous = row
    current_policy.write_text(current_policy.read_text() + "\n# revised policy")
    context = rows(local, "cma_contexts")[0]
    local.local_a2a.scheduler_trigger.schedule(str(context["context_id"]), "policy-updated")
    drain(local)
    row = rows(local, "cma_input_requests")[0]
    assert row["request_id"] != previous["request_id"]
    assert message["blocks"] == []
    message = next(
        m for m in local.slack.messages.values() if "agent_tool_allow" in json.dumps(m.get("blocks"))
    )
    assert row["request_id"] in json.dumps(message["blocks"])
    signed(local, stale_action, interaction=True)
    drain(local)
    assert local.local_a2a.provider.confirm_calls == 0
    signed(local, action("U002", ruling), interaction=True)
    drain(local)
    assert rows(local, "cma_input_requests")[0]["human_ruling"] is None
    accepted = action("U001", ruling)
    signed(local, accepted, interaction=True)
    signed(local, accepted, interaction=True)
    drain(local)
    signed(local, action("U001", "deny" if ruling == "allow" else "allow"), interaction=True)
    drain(local)
    final = rows(local, "cma_input_requests")[0]
    assert final["human_ruling"] == final["decision"] == ruling
    assert final["status"] == "resolved" and local.local_a2a.provider.confirm_calls == 1
    assert message["blocks"] == []
    assert rows(local, "cma_tasks")[0]["a2a_state"] == "COMPLETED"
