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
from managed_agents_app.cma_controller.push_trigger import AsyncLocalPushTrigger
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
async def test_push_handler_partial_batch_and_maintenance(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    closed = 0

    class Dispatcher:
        async def run_context(self, context_id: str) -> None:
            seen.append(context_id)
            if context_id == "bad":
                raise RuntimeError("injected worker failure")

        async def maintenance(self) -> int:
            return 3

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
    assert await push_handler._handle({"maintenance": "a2a-push"}) == {"scheduled": 3}
    assert closed == 2
