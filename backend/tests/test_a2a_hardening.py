from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from a2a.client.errors import A2AClientError, A2AClientTimeoutError
from a2a.utils.errors import InvalidParamsError

from managed_agents_app.a2a_client.client import (
    AmbiguousA2ATransportError,
    DefinitiveA2ARequestError,
    _send_error,
)
from managed_agents_app.cma_controller import push_handler
from managed_agents_app.cma_controller.app import agent_card, create_app
from managed_agents_app.cma_controller.push_trigger import AsyncLocalPushTrigger, SqsPushTrigger
from managed_agents_app.domain import A2ATaskUpdated
from managed_agents_app.testing.local_event_bus import LocalEventBus


def test_a2a_task_updated_has_metadata_only_camel_case_wire() -> None:
    event = A2ATaskUpdated(
        delivery_id="delivery",
        agent_id="agent",
        task_id="task",
        thread_id="thread",
        context_id="context",
        event_kind="status_update",
        task_state="COMPLETED",
    )
    expected = {
        "version": 1,
        "deliveryId": "delivery",
        "agentId": "agent",
        "taskId": "task",
        "threadId": "thread",
        "contextId": "context",
        "eventKind": "status_update",
        "taskState": "COMPLETED",
    }
    assert event.boundary() == expected
    bus = LocalEventBus()
    bus.publish("app.a2a", "A2ATaskUpdated", event)
    assert bus.snapshot()["pending"][0]["detail"] == expected


def test_send_error_classification_preserves_uncertain_admission() -> None:
    assert isinstance(_send_error(InvalidParamsError("rejected")), DefinitiveA2ARequestError)
    assert isinstance(_send_error(A2AClientTimeoutError("lost")), AmbiguousA2ATransportError)
    assert isinstance(_send_error(A2AClientError("unknown")), AmbiguousA2ATransportError)
    request = httpx.Request("POST", "http://controller/message:send")
    status = httpx.HTTPStatusError("rejected", request=request, response=httpx.Response(403, request=request))
    sdk = A2AClientError("HTTP Error 403")
    sdk.__cause__ = status
    assert isinstance(_send_error(sdk), DefinitiveA2ARequestError)
    status = httpx.HTTPStatusError(
        "uncertain", request=request, response=httpx.Response(503, request=request)
    )
    sdk.__cause__ = status
    assert isinstance(_send_error(sdk), AmbiguousA2ATransportError)


def test_agent_card_does_not_advertise_unrunnable_push() -> None:
    service = SimpleNamespace(
        repository=SimpleNamespace(
            config=SimpleNamespace(cma_a2a_push_allowed_url="https://sink.test/events")
        )
    )
    with pytest.raises(RuntimeError, match="runnable push worker"):
        create_app(service)
    card = agent_card("http://controller.test")
    assert not card.capabilities.push_notifications
    assert all("async-copilot-controller" not in item.uri for item in card.capabilities.extensions)


def test_push_retry_uses_sqs_delay_without_changing_wakeup_wire() -> None:
    from unittest.mock import Mock

    sqs = Mock()
    trigger = SqsPushTrigger("queue", sqs)
    trigger.schedule_context("context", "push-retry", 30)
    request = sqs.send_message.call_args.kwargs
    assert request["QueueUrl"] == "queue" and request["DelaySeconds"] == 30
    assert json.loads(request["MessageBody"]) == {
        "version": 1,
        "contextId": "context",
        "reason": "push-retry",
    }
    with pytest.raises(ValueError, match="Invalid push wakeup"):
        trigger.schedule_context("context", "push-retry", 901)
    sqs.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_async_local_push_worker_drains_and_survives_failure() -> None:
    trigger = AsyncLocalPushTrigger()
    seen: list[str] = []
    done = asyncio.Event()

    class Dispatcher:
        async def run_context(self, context_id: str) -> None:
            seen.append(context_id)
            if context_id == "fails":
                raise RuntimeError("injected failure")
            done.set()

    trigger.schedule_context("fails", "before-start")
    trigger.schedule_context("works", "before-start")
    trigger.schedule_context("works", "duplicate")
    trigger.start(Dispatcher())
    await asyncio.wait_for(done.wait(), timeout=1)
    await trigger.stop()
    assert seen == ["fails", "works"]


@pytest.mark.asyncio
async def test_push_handler_partial_batch_and_rejects_maintenance(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    closed = 0

    class Dispatcher:
        async def run_context(self, context_id: str) -> None:
            seen.append(context_id)
            if context_id == "bad":
                raise RuntimeError("injected worker failure")

    class Composition:
        push_dispatcher = Dispatcher()

        async def close(self) -> None:
            nonlocal closed
            closed += 1

    monkeypatch.setattr(
        push_handler,
        "load_config",
        lambda: SimpleNamespace(cma_push_queue_url="queue", cma_pending_input_bucket="bucket"),
    )
    monkeypatch.setattr(push_handler, "build_controller", lambda _config: Composition())
    result = await push_handler._handle(
        {
            "Records": [
                {"messageId": "1", "body": json.dumps({"version": 1, "contextId": "good", "reason": "test"})},
                {"messageId": "2", "body": json.dumps({"version": 1, "contextId": "bad", "reason": "test"})},
                {
                    "messageId": "3",
                    "body": json.dumps({"version": 1, "contextId": "good", "reason": "duplicate"}),
                },
            ]
        }
    )
    assert result == {"batchItemFailures": [{"itemIdentifier": "2"}]}
    assert seen == ["good", "bad", "good"]
    with pytest.raises(ValueError, match="Unknown push worker invocation"):
        await push_handler._handle({"maintenance": "a2a-push"})
    assert closed == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["task", "message", "status_update", "artifact_update"])
async def test_sink_relays_metadata_without_a2a_reads(monkeypatch, kind):
    from unittest.mock import AsyncMock, Mock

    from a2a.types import a2a_pb2 as a2a
    from google.protobuf.json_format import MessageToJson

    from managed_agents_app.a2a_client.client import A2AControllerClient
    from managed_agents_app.a2a_event_sink.app import create_app as create_sink
    from managed_agents_app.agents.registry import AgentRegistration, AgentRegistry
    from managed_agents_app.db.thread_repository import ThreadRepository

    get_task = AsyncMock(side_effect=AssertionError("The receiver must not read A2A history"))
    monkeypatch.setattr(A2AControllerClient, "get_task", get_task)
    threads = Mock(spec=ThreadRepository)
    threads.get_task_binding.return_value = {"thread_id": "thread"}
    threads.get_thread.return_value = {"thread_id": "thread", "agent_id": "cma", "context_id": "context"}
    registry = AgentRegistry([AgentRegistration("cma", "http://controller.test")])
    bus = LocalEventBus()
    envelope = a2a.StreamResponse()
    payload = getattr(envelope, kind)
    if kind == "task":
        payload.id = "task"
        payload.status.state = a2a.TaskState.TASK_STATE_COMPLETED
        payload.history.add(message_id="private-message", parts=[a2a.Part(text="private transcript")])
    else:
        payload.task_id = "task"
        if kind == "status_update":
            payload.status.state = a2a.TaskState.TASK_STATE_WORKING
        elif kind == "message":
            payload.parts.add(text="private transcript")
        else:
            payload.artifact.parts.add(text="private tool output")
    payload.context_id = "context"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_sink(threads, registry, bus)), base_url="http://sink"
    ) as http:
        for _ in range(2):
            response = await http.post(
                "/internal/a2a/events/cma",
                content=MessageToJson(envelope),
                headers={"Content-Type": "application/a2a+json"},
            )
            assert response.status_code == 200
    events = bus.snapshot()["pending"]
    assert len(events) == 2
    assert events[0]["detail"] == events[1]["detail"]
    detail = events[0]["detail"]
    assert detail["taskId"] == "task" and detail["threadId"] == "thread"
    assert "private" not in json.dumps(detail)
    assert set(detail) == {
        "version",
        "deliveryId",
        "agentId",
        "taskId",
        "threadId",
        "contextId",
        "eventKind",
    } | ({"taskState"} if kind in {"task", "status_update"} else set())
    get_task.assert_not_called()
    assert {call[0] for call in threads.mock_calls} == {"get_task_binding", "get_thread"}


@pytest.mark.asyncio
async def test_sink_rejects_invalid_callbacks_and_failed_eventbridge_publication(monkeypatch):
    from unittest.mock import Mock

    from managed_agents_app import events
    from managed_agents_app.a2a_event_sink.app import create_app as create_sink
    from managed_agents_app.agents.registry import AgentRegistration, AgentRegistry
    from managed_agents_app.db.thread_repository import ThreadRepository

    threads = Mock(spec=ThreadRepository)
    threads.get_task_binding.return_value = {"thread_id": "thread"}
    threads.get_thread.return_value = {"thread_id": "thread", "agent_id": "cma", "context_id": "context"}
    publisher = Mock()
    publisher.put_events.side_effect = [{"FailedEntryCount": 1}, {"FailedEntryCount": 0}]
    monkeypatch.setattr(events, "_client", lambda: publisher)
    sink = create_sink(
        threads,
        AgentRegistry([AgentRegistration("cma", "http://controller")]),
        events.EventBridgeEventBus("bus"),
    )
    valid = {"task": {"id": "task", "contextId": "context"}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=sink), base_url="http://sink") as http:
        path = "/internal/a2a/events/cma"
        assert (await http.post(path, json=valid)).status_code == 415
        http.headers["Content-Type"] = "application/a2a+json"
        for body in (
            b"not-json",
            b"\xff",
            b"{}",
            b'{"task": {"id": "task"}}',
            b'{"task": {}, "message": {}}',
        ):
            assert (await http.post(path, content=body)).status_code == 400
        assert (
            await http.post(path, json=valid, headers={"X-A2A-Delivery-Id": "not-a-credential"})
        ).status_code == 400
        assert (await http.post("/internal/a2a/events/unknown", json=valid)).status_code == 400
        assert (await http.post(path, json={"task": {"id": "task", "contextId": "other"}})).status_code == 409

        async def oversized():
            yield b" " * (1024 * 1024)
            yield b" "

        assert (await http.post(path, content=oversized())).status_code == 413
        threads.get_task_binding.return_value = None
        assert (await http.post(path, json=valid)).status_code == 503
        threads.get_task_binding.return_value = {"thread_id": "thread"}
        threads.get_thread.return_value["agent_id"] = "other"
        assert (await http.post(path, json=valid)).status_code == 409
        threads.get_thread.return_value["agent_id"] = "cma"
        publisher.put_events.assert_not_called()
        assert (await http.post(path, json=valid)).status_code == 503
        assert (await http.post(path, json=valid)).status_code == 200
        assert publisher.put_events.call_count == 2
        assert "taskState" not in json.loads(publisher.put_events.call_args.kwargs["Entries"][0]["Detail"])


@pytest.mark.asyncio
async def test_local_push_keeps_a_wakeup_arriving_during_dispatch():
    trigger = AsyncLocalPushTrigger()
    completed = asyncio.Event()
    calls = 0

    class Dispatcher:
        async def run_context(self, context_id):
            nonlocal calls
            calls += 1
            if calls == 1:
                trigger.schedule_context(context_id, "changed-during-post")
                await asyncio.sleep(0)
            else:
                completed.set()

    trigger.start(Dispatcher())
    try:
        trigger.schedule_context("context", "first")
        await asyncio.wait_for(completed.wait(), 1)
        assert calls == 2
    finally:
        await trigger.stop()
