from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Body
from psycopg import sql

from managed_agents_app.db.connection import connect
from managed_agents_app.runtime import Runtime
from managed_agents_app.testing.factory import validate_local_database
from managed_agents_app.testing.fake_agent import FakeManagedAgent
from managed_agents_app.testing.faults import FaultInjector
from managed_agents_app.testing.local_event_bus import LocalEventBus
from managed_agents_app.testing.local_queue import LocalQueue
from managed_agents_app.testing.recording_slack import RecordingSlack

TABLES = (
    "principals",
    "external_identities",
    "agent_sessions",
    "surface_bindings",
    "ingress_events",
    "projection_events",
    "agent_feedback",
    "slack_response_streams",
)


def router(runtime: Runtime) -> APIRouter:
    validate_local_database(runtime.config)
    api = APIRouter(prefix="/_test")
    agent = cast(FakeManagedAgent, runtime.agent)
    slack = cast(RecordingSlack, runtime.slack)
    bus = cast(LocalEventBus, runtime.events)
    queue = cast(LocalQueue, bus.agent_input_queue)
    faults = cast(FaultInjector, runtime.faults)

    @api.post("/reset")
    def reset() -> dict[str, bool]:
        validate_local_database(runtime.config)
        faults.reset()
        agent.reset()
        bus.reset()
        queue.reset()
        # Released handlers can finish while drain quiesces. Clear any final
        # fake mutations they made after cancellation before reseeding state.
        agent.reset()
        slack.reset()
        with connect(runtime.config) as conn:
            conn.execute(
                sql.SQL("TRUNCATE {} CASCADE").format(sql.SQL(", ").join(map(sql.Identifier, TABLES)))
            )
        runtime.db.ensure_principal(runtime.config.dev_principal_id)
        runtime.db.map_external_identity(runtime.config.dev_principal_id, "web", "development", "single-user")
        runtime.db.map_external_identity(runtime.config.dev_principal_id, "slack", "T001", "U001")
        other = "00000000-0000-4000-8000-000000000002"
        runtime.db.ensure_principal(other)
        runtime.db.map_external_identity(other, "slack", "T001", "U002")
        return {"ok": True}

    @api.get("/state")
    def state() -> dict[str, Any]:
        with connect(runtime.config) as conn:
            database = {
                table: conn.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))).fetchall()
                for table in TABLES
            }
        return {
            "agent": agent.snapshot(),
            "slack": slack.snapshot(),
            "events": bus.snapshot(),
            "queue": queue.snapshot(),
            "database": database,
            "faults": faults.snapshot(),
        }

    @api.post("/agent/script")
    def script(body: Annotated[dict[str, Any], Body()]) -> dict[str, bool]:
        events = body.get(
            "events",
            [
                {
                    "type": "agent.message",
                    "content": [{"type": "text", "text": body.get("response", "Hello from Claude")}],
                },
                {"type": "session.status_idle", "stop_reason": {"type": "end_turn"}},
            ],
        )
        agent.script(body["prompt"], events, body.get("automatic", True))
        return {"ok": True}

    @api.post("/agent/script-tool-approval")
    def approval(body: Annotated[dict[str, Any], Body()]) -> dict[str, bool]:
        agent.approval_scripts[f"{body['tool_id']}:{body['approved']}"] = {
            "events": body["events"],
            "automatic": body.get("automatic", True),
        }
        return {"ok": True}

    @api.post("/agent/script-interrupt")
    def script_interrupt(body: Annotated[dict[str, Any], Body()]) -> dict[str, bool]:
        agent.interrupt_events = body["events"]
        return {"ok": True}

    @api.post("/agent/advance")
    def advance(body: Annotated[dict[str, Any], Body()]) -> dict[str, bool]:
        agent.advance(body["session_id"], body.get("count"))
        agent.flush_notifications()
        return {"ok": True}

    @api.post("/agent/emit")
    def emit(body: Annotated[dict[str, Any], Body()]) -> dict[str, str]:
        return {"id": agent.emit(body["session_id"], body["event"])}

    @api.post("/events/drain")
    def drain(body: Annotated[dict[str, Any] | None, Body()] = None) -> dict[str, Any]:
        body = body or {}
        max_events = min(max(int(body.get("max_events", 100)), 1), 1000)
        workers = min(max(int(body.get("workers", 1)), 1), 4)
        for _ in range(20):
            agent.flush_notifications()
            bus.drain(max_events, workers=workers)
            queue.drain(max_events, workers=workers)
            agent.flush_notifications()
            result = bus.snapshot()
            queue_state = queue.snapshot()
            queue_has_visible_messages = any(item["state"] == "visible" for item in queue_state["messages"])
            if not result["pending"] and not queue_has_visible_messages:
                return {**result, "queue": queue_state}
        raise RuntimeError("Local event and SQS queues did not quiesce")

    @api.post("/queue/drain")
    def queue_drain(body: Annotated[dict[str, Any] | None, Body()] = None) -> dict[str, Any]:
        body = body or {}
        return queue.drain(
            min(max(int(body.get("max_messages", 100)), 1), 1000),
            workers=min(max(int(body.get("workers", 1)), 1), 4),
        )

    @api.post("/queue/advance")
    def queue_advance(body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        return queue.advance_time(int(body["seconds"]))

    @api.post("/queue/duplicate")
    def queue_duplicate(body: Annotated[dict[str, Any], Body()]) -> dict[str, str]:
        return {"id": queue.duplicate(str(body["id"]))}

    @api.post("/queue/send-raw")
    def queue_send_raw(body: Annotated[dict[str, Any], Body()]) -> dict[str, str]:
        return {"id": queue.send_raw(str(body["body"]))}

    @api.post("/events/retry")
    def retry(body: Annotated[dict[str, Any], Body()]) -> dict[str, bool]:
        bus.retry(int(body["id"]))
        return {"ok": True}

    @api.post("/fail/event")
    def fail_event(body: Annotated[dict[str, Any], Body()]) -> dict[str, bool]:
        bus.failures[body["detail_type"]] = body.get("times", 1)
        return {"ok": True}

    @api.post("/fail/slack")
    def fail_slack(body: Annotated[dict[str, Any], Body()]) -> dict[str, bool]:
        slack.fail_next(
            body["operation"],
            body.get("error", "rate_limited"),
            after=body.get("after", False),
            skip=body.get("skip", 0),
        )
        return {"ok": True}

    @api.post("/faults")
    def arm_fault(body: Annotated[dict[str, Any], Body()]) -> dict[str, bool]:
        faults.arm(body["point"], body.get("key", ""), body.get("behavior", "raise"), body.get("times", 1))
        return {"ok": True}

    @api.post("/faults/release")
    def release_fault(body: Annotated[dict[str, Any], Body()]) -> dict[str, bool]:
        faults.release(body["point"], body.get("key", ""))
        return {"ok": True}

    @api.get("/events")
    def events() -> dict[str, Any]:
        return bus.snapshot()

    @api.get("/agent/events")
    def agent_events(session_id: str) -> list[dict[str, Any]]:
        return agent.list_events(session_id)

    @api.get("/slack/{kind}")
    def slack_state(kind: str) -> Any:
        return slack.snapshot().get(kind, [])

    # Convenience injection still goes through timestamped HMAC verification.
    @api.post("/slack/{kind}")
    def inject(kind: str, body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        import hashlib
        import hmac
        import json
        import time
        from urllib.parse import urlencode

        from managed_agents_app.handlers.slack_ingress import handle_request

        raw = urlencode({"payload": json.dumps(body)}) if kind == "interaction" else json.dumps(body)
        timestamp = str(int(time.time()))
        signature = hmac.new(
            (runtime.config.slack_signing_secret or "").encode(),
            f"v0:{timestamp}:{raw}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return handle_request(
            runtime,
            {
                "body": raw,
                "headers": {
                    "content-type": "application/x-www-form-urlencoded"
                    if kind == "interaction"
                    else "application/json",
                    "x-slack-request-timestamp": timestamp,
                    "x-slack-signature": f"v0={signature}",
                },
            },
        )

    return api
