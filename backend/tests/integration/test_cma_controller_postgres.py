from __future__ import annotations

import asyncio
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import httpx
import pytest
from a2a.client.client import ClientCallContext, ClientConfig
from a2a.client.client_factory import ClientFactory
from a2a.extensions.common import HTTP_EXTENSION_HEADER
from a2a.types import a2a_pb2 as a2a
from a2a.utils.errors import InvalidParamsError, TaskNotFoundError
from google.protobuf.json_format import MessageToDict, Parse  # type: ignore[import-untyped]

from managed_agents_app.cma_controller import app as controller_app
from managed_agents_app.cma_controller.admin import ControllerAdmin
from managed_agents_app.cma_controller.app import create_app
from managed_agents_app.cma_controller.pending_input import FilePendingInputStore
from managed_agents_app.cma_controller.runtime_repository import (
    ControllerRuntimeRepository,
    LostSchedulerClaim,
)
from managed_agents_app.cma_controller.scheduler import CmaScheduler, approval_request_id
from managed_agents_app.cma_controller.service import ControllerService
from managed_agents_app.cma_controller.task_view import TaskViewOptions
from managed_agents_app.cma_controller.trigger import AsyncLocalSchedulerTrigger, LocalSchedulerTrigger
from managed_agents_app.config import load_config
from managed_agents_app.db.connection import connect
from managed_agents_app.policy.models import PolicyError
from managed_agents_app.policy.openappa import OpenAppaPolicyEngine
from managed_agents_app.protocols.controller_profile import HUMAN_INPUT_EXTENSION_URI
from managed_agents_app.testing.factory import validate_local_database
from managed_agents_app.testing.fake_cma_provider import FakeCmaProvider

pytestmark = pytest.mark.skipif(os.getenv("RUN_POSTGRES_TESTS") != "1", reason="Run through npm run e2e")

HUMAN_INPUT_OPTIONS = TaskViewOptions(enabled_extensions=frozenset({HUMAN_INPUT_EXTENSION_URI}))
HUMAN_INPUT_CALL = ClientCallContext(service_parameters={HTTP_EXTENSION_HEADER: HUMAN_INPUT_EXTENSION_URI})


@pytest.fixture
def controller(tmp_path: Path):
    config = load_config()
    validate_local_database(config)
    repository = ControllerRuntimeRepository(config)
    provider = FakeCmaProvider()
    pending = FilePendingInputStore(tmp_path / "pending")
    trigger = LocalSchedulerTrigger()
    service = ControllerService(repository, provider, pending, trigger)
    scheduler = CmaScheduler(
        repository,
        provider,
        pending,
        trigger,
        policy_engine=OpenAppaPolicyEngine(
            str(Path(__file__).resolve().parents[3] / ".generated/openappa-bin/openappa-cma"),
            str(Path(__file__).resolve().parents[3] / "services/openappa/tests/baseline.toml"),
        ),
    )
    yield repository, provider, pending, trigger, service, scheduler
    assert provider.stream_calls == 0, "CMA SSE is P1-only"


def watch_started(service, monkeypatch):
    """Coordinate persisted events with an A2A snapshot, independent of provider SSE."""
    started = asyncio.Event()
    original = service.watch_marker

    async def marker(task_id):
        result = await original(task_id)
        started.set()
        return result

    monkeypatch.setattr(service, "watch_marker", marker)
    return started


def message(text: str, context_id: str = "", message_id: str | None = None) -> a2a.Message:
    return a2a.Message(
        message_id=message_id or str(uuid.uuid4()),
        context_id=context_id,
        role=a2a.Role.ROLE_USER,
        parts=[a2a.Part(text=text)],
    )


class UnavailablePolicy:
    async def evaluate_tool_call(self, request, timeout):
        raise PolicyError("policy-runtime-failure")


def emit_calls(provider, session_id, calls):
    for event_id, name in calls:
        provider.emit(session_id, {"id": event_id, "type": "agent.tool_use", "name": name, "input": {}})
    provider.emit(
        session_id,
        {
            "type": "session.status_idle",
            "stop_reason": {"type": "requires_action", "event_ids": [item[0] for item in reversed(calls)]},
        },
    )


def tool_response(task, event_id, decision):
    item = a2a.Message(
        message_id=str(uuid.uuid4()),
        task_id=task.id,
        context_id=task.context_id,
        role=a2a.Role.ROLE_USER,
        extensions=[HUMAN_INPUT_EXTENSION_URI],
    )
    item.metadata.update(
        {
            HUMAN_INPUT_EXTENSION_URI: {
                "response": {
                    "kind": "tool_approval",
                    "requestId": approval_request_id(task.id, event_id),
                    "decision": decision,
                }
            }
        }
    )
    return item


@pytest.mark.asyncio
async def test_official_http_json_client_and_durable_fifo(controller) -> None:
    repository, provider, pending, _trigger, service, scheduler = controller
    app = create_app(service, "http://a2a.test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a2a.test") as http:
        client = await ClientFactory(
            ClientConfig(httpx_client=http, supported_protocol_bindings=["HTTP+JSON"], streaming=False)
        ).create_from_url("http://a2a.test")
        first_message = message("first")
        request = a2a.SendMessageRequest(
            message=first_message, configuration=a2a.SendMessageConfiguration(return_immediately=True)
        )
        first = [response async for response in client.send_message(request)][0].task
        duplicate = [response async for response in client.send_message(request)][0].task
        assert first.id == duplicate.id and first.context_id == duplicate.context_id
        assert first.status.state == a2a.TaskState.TASK_STATE_SUBMITTED
        assert [(item.message_id, item.parts[0].text) for item in first.history] == [
            (first_message.message_id, "first")
        ]
        assert repository.get_task(first.id)["dispatch_attempts"] == 0
        await scheduler.run_once(first.context_id)
        assert repository.get_task(first.id)["dispatch_attempts"] == 1
        completed = await client.get_task(a2a.GetTaskRequest(id=first.id))
        assert completed.status.state == a2a.TaskState.TASK_STATE_COMPLETED
        assert completed.status.message.parts[0].text == "Done"
        assert len(completed.history) == 2
        assert provider.create_calls == 1
        assert pending.list_keys() == []

        later = [
            response
            async for response in client.send_message(
                a2a.SendMessageRequest(
                    message=message("second", first.context_id),
                    configuration=a2a.SendMessageConfiguration(return_immediately=True),
                )
            )
        ][0].task
        await scheduler.run_once(first.context_id)
        await scheduler.run_once(first.context_id)
        assert (await client.get_task(a2a.GetTaskRequest(id=later.id))).status.state == (
            a2a.TaskState.TASK_STATE_COMPLETED
        )
        listed = await client.list_tasks(a2a.ListTasksRequest(context_id=first.context_id, page_size=10))
        assert [task.id for task in listed.tasks] == [first.id, later.id]
        assert listed.total_size == 2
        page_one = await client.list_tasks(a2a.ListTasksRequest(context_id=first.context_id, page_size=1))
        page_two = await client.list_tasks(
            a2a.ListTasksRequest(
                context_id=first.context_id, page_size=1, page_token=page_one.next_page_token
            )
        )
        assert [page_one.tasks[0].id, page_two.tasks[0].id] == [first.id, later.id]


@pytest.mark.asyncio
async def test_official_blocking_send_reaches_completion_or_input_required(controller) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    app = create_app(service, "http://a2a.test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a2a.test") as http:
        client = await ClientFactory(
            ClientConfig(httpx_client=http, supported_protocol_bindings=["HTTP+JSON"], streaming=False)
        ).create_from_url("http://a2a.test")

        async def complete_when_admitted(message_id: str, needs_input: bool) -> None:
            for _ in range(100):
                context = repository.find_context_by_creation_message_id(message_id)
                if context:
                    context_id = str(context["context_id"])
                    await scheduler.run_once(context_id)
                    if needs_input:
                        session_id = str(repository.get_context(context_id)["cma_session_id"])
                        provider.emit(
                            session_id,
                            {
                                "id": "blocking-tool",
                                "type": "agent.custom_tool_use",
                                "name": "ask_user",
                                "input": {"prompt": "Continue?"},
                            },
                        )
                        provider.emit(
                            session_id,
                            {
                                "type": "session.status_idle",
                                "stop_reason": {"type": "requires_action", "event_ids": ["blocking-tool"]},
                            },
                        )
                        await scheduler.run_once(context_id)
                    return
                await asyncio.sleep(0.02)
            raise AssertionError("Blocking task was not admitted")

        completed_message = message("blocking completion")
        wake = asyncio.create_task(complete_when_admitted(completed_message.message_id, False))
        completed = [
            response
            async for response in client.send_message(a2a.SendMessageRequest(message=completed_message))
        ][0].task
        await wake
        assert completed.status.state == a2a.TaskState.TASK_STATE_COMPLETED

        provider.automatic = False
        blocked_message = message("blocking approval")
        wake = asyncio.create_task(complete_when_admitted(blocked_message.message_id, True))
        blocked = [
            response
            async for response in client.send_message(a2a.SendMessageRequest(message=blocked_message))
        ][0].task
        await wake
        assert blocked.status.state == a2a.TaskState.TASK_STATE_INPUT_REQUIRED
        assert blocked.status.message.parts[0].text == "This task requires user input."


@pytest.mark.asyncio
async def test_blocking_client_disconnect_does_not_cancel_durable_task(controller) -> None:
    repository, _provider, _pending, _trigger, service, scheduler = controller
    app = create_app(service, "http://a2a.test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a2a.test") as http:
        client = await ClientFactory(
            ClientConfig(httpx_client=http, supported_protocol_bindings=["HTTP+JSON"], streaming=False)
        ).create_from_url("http://a2a.test")
        inbound = message("survive disconnect")

        async def collect() -> list[a2a.StreamResponse]:
            return [item async for item in client.send_message(a2a.SendMessageRequest(message=inbound))]

        request = asyncio.create_task(collect())
        for _ in range(100):
            context = repository.find_context_by_creation_message_id(inbound.message_id)
            if context:
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("Task was not admitted")
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        context_id = str(context["context_id"])
        task = repository.find_task_by_message_id(context_id, inbound.message_id)
        assert task is not None
        await scheduler.run_once(context_id)
        final = await client.get_task(a2a.GetTaskRequest(id=str(task["task_id"])))
        assert final.status.state == a2a.TaskState.TASK_STATE_COMPLETED


@pytest.mark.asyncio
async def test_wire_extension_negotiation_and_spoofed_continuation(controller) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    provider.automatic = False
    task = await service.send(message("approval turn"))
    await scheduler.run_once(task.context_id)
    session_id = str(repository.get_context(task.context_id)["cma_session_id"])
    provider.emit(
        session_id,
        {
            "id": "negotiated-tool",
            "type": "agent.custom_tool_use",
            "name": "ask_user",
            "input": {"prompt": "Continue?"},
        },
    )
    provider.emit(
        session_id,
        {
            "type": "session.status_idle",
            "stop_reason": {"type": "requires_action", "event_ids": ["negotiated-tool"]},
        },
    )
    await scheduler.run_once(task.context_id)
    app = create_app(service, "http://a2a.test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a2a.test") as http:
        client = await ClientFactory(
            ClientConfig(httpx_client=http, supported_protocol_bindings=["HTTP+JSON"], streaming=False)
        ).create_from_url("http://a2a.test")
        plain = await client.get_task(a2a.GetTaskRequest(id=task.id))
        assert plain.status.state == a2a.TaskState.TASK_STATE_INPUT_REQUIRED
        assert not plain.status.message.extensions
        assert plain.status.message.parts[0].text == "This task requires user input."
        unknown = await client.get_task(
            a2a.GetTaskRequest(id=task.id),
            context=ClientCallContext(service_parameters={HTTP_EXTENSION_HEADER: "urn:unknown-extension"}),
        )
        assert not unknown.status.message.extensions
        activated = await client.get_task(a2a.GetTaskRequest(id=task.id), context=HUMAN_INPUT_CALL)
        assert list(activated.status.message.extensions) == [HUMAN_INPUT_EXTENSION_URI]
        envelope = activated.status.message.metadata[HUMAN_INPUT_EXTENSION_URI]
        assert len(envelope["requests"]) == 1

        approval = a2a.Message(
            message_id=str(uuid.uuid4()),
            task_id=task.id,
            context_id=task.context_id,
            role=a2a.Role.ROLE_USER,
            extensions=[HUMAN_INPUT_EXTENSION_URI],
        )
        approval.metadata.update(
            {
                HUMAN_INPUT_EXTENSION_URI: {
                    "response": {
                        "kind": "clarification",
                        "requestId": approval_request_id(task.id, "negotiated-tool"),
                        "answer": "yes",
                    }
                }
            }
        )
        request = a2a.SendMessageRequest(
            message=approval, configuration=a2a.SendMessageConfiguration(return_immediately=True)
        )
        with pytest.raises(InvalidParamsError):
            [item async for item in client.send_message(request)]
        assert repository.list_input_requests(task.id)[0]["status"] == "pending"
        request.message.parts.append(a2a.Part(text="invalid continuation text"))
        with pytest.raises(InvalidParamsError):
            [item async for item in client.send_message(request, context=HUMAN_INPUT_CALL)]
        request.message.parts.clear()
        accepted = [item async for item in client.send_message(request, context=HUMAN_INPUT_CALL)][0].task
        assert accepted.id == task.id
        assert repository.list_input_requests(task.id)[0]["status"] == "resolving"


@pytest.mark.asyncio
async def test_local_asgi_scheduler_executes_admitted_work(controller) -> None:
    repository, provider, pending, _trigger, _service, _scheduler = controller
    trigger = AsyncLocalSchedulerTrigger()
    service = ControllerService(repository, provider, pending, trigger)
    app = create_app(
        service, "http://a2a.test", local_scheduler=CmaScheduler(repository, provider, pending, trigger)
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a2a.test") as http,
    ):
        client = await ClientFactory(
            ClientConfig(httpx_client=http, supported_protocol_bindings=["HTTP+JSON"], streaming=False)
        ).create_from_url("http://a2a.test")
        accepted = [
            item
            async for item in client.send_message(
                a2a.SendMessageRequest(
                    message=message("local auto"),
                    configuration=a2a.SendMessageConfiguration(return_immediately=True),
                )
            )
        ][0].task
        for _ in range(100):
            final = await client.get_task(a2a.GetTaskRequest(id=accepted.id))
            if final.status.state == a2a.TaskState.TASK_STATE_COMPLETED:
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("Local scheduler did not execute admitted task")
        assert final.history[-1].parts[0].text == "Done"


@pytest.mark.asyncio
async def test_production_app_local_configuration_drains_wakeups(
    controller, monkeypatch, tmp_path: Path
) -> None:
    repository, provider, _pending, _trigger, _service, _scheduler = controller
    config = repository.config
    monkeypatch.setattr(controller_app, "load_config", lambda: config)
    from managed_agents_app.cma_controller.composition import build_controller

    monkeypatch.setattr(
        controller_app,
        "build_controller",
        lambda _config: build_controller(config, repository=repository, provider=provider),
    )
    monkeypatch.setenv("CMA_PENDING_INPUT_DIR", str(tmp_path))
    app = controller_app.production_app()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8081") as http,
    ):
        client = await ClientFactory(
            ClientConfig(httpx_client=http, supported_protocol_bindings=["HTTP+JSON"], streaming=False)
        ).create_from_url("http://127.0.0.1:8081")
        initial = [
            item
            async for item in client.send_message(
                a2a.SendMessageRequest(
                    message=message("production local"),
                    configuration=a2a.SendMessageConfiguration(return_immediately=True),
                )
            )
        ][0].task
        for _ in range(100):
            current = await client.get_task(a2a.GetTaskRequest(id=initial.id))
            if current.status.state == a2a.TaskState.TASK_STATE_COMPLETED:
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("production_app local scheduler did not run")


@pytest.mark.asyncio
async def test_wire_rejects_invalid_message_shapes_and_pagination(controller) -> None:
    _repository, _provider, _pending, _trigger, service, _scheduler = controller
    app = create_app(service, "http://a2a.test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a2a.test") as http:
        client = await ClientFactory(
            ClientConfig(httpx_client=http, supported_protocol_bindings=["HTTP+JSON"], streaming=False)
        ).create_from_url("http://a2a.test")
        good = message("baseline")
        request = a2a.SendMessageRequest(
            message=good, configuration=a2a.SendMessageConfiguration(return_immediately=True)
        )
        task = [item async for item in client.send_message(request)][0].task

        invalid_messages = [
            a2a.Message(message_id=str(uuid.uuid4()), role=a2a.Role.ROLE_AGENT, parts=[a2a.Part(text="x")]),
            a2a.Message(role=a2a.Role.ROLE_USER, parts=[a2a.Part(text="x")]),
            a2a.Message(message_id=str(uuid.uuid4()), role=a2a.Role.ROLE_USER),
            a2a.Message(message_id=str(uuid.uuid4()), role=a2a.Role.ROLE_USER, parts=[a2a.Part(raw=b"x")]),
            a2a.Message(
                message_id=str(uuid.uuid4()),
                role=a2a.Role.ROLE_USER,
                task_id=task.id,
                context_id=task.context_id,
                parts=[a2a.Part(text="ordinary continuation")],
            ),
            a2a.Message(
                message_id=str(uuid.uuid4()),
                role=a2a.Role.ROLE_USER,
                extensions=[HUMAN_INPUT_EXTENSION_URI],
                parts=[a2a.Part(text="no task")],
            ),
        ]
        for invalid in invalid_messages:
            with pytest.raises(InvalidParamsError):
                [
                    item
                    async for item in client.send_message(
                        a2a.SendMessageRequest(
                            message=invalid,
                            configuration=a2a.SendMessageConfiguration(return_immediately=True),
                        )
                    )
                ]

        unknown = message("unknown", str(uuid.uuid4()), good.message_id)
        with pytest.raises(InvalidParamsError):
            [
                item
                async for item in client.send_message(
                    a2a.SendMessageRequest(
                        message=unknown,
                        configuration=a2a.SendMessageConfiguration(return_immediately=True),
                    )
                )
            ]
        for page in [
            a2a.ListTasksRequest(context_id=task.context_id, page_size=-1),
            a2a.ListTasksRequest(context_id=task.context_id, page_size=101),
            a2a.ListTasksRequest(context_id=task.context_id, page_token="bad"),
            a2a.ListTasksRequest(context_id=task.context_id, page_token="-1"),
        ]:
            with pytest.raises(InvalidParamsError):
                await client.list_tasks(page)
        with pytest.raises(InvalidParamsError):
            await client.get_task(a2a.GetTaskRequest(id=task.id, history_length=-1))
        with pytest.raises(TaskNotFoundError):
            await client.get_task(a2a.GetTaskRequest(id=str(uuid.uuid4())))


@pytest.mark.asyncio
async def test_official_stream_and_reconnect_after_controller_restart(controller) -> None:
    repository, provider, pending, trigger, service, scheduler = controller
    app = create_app(service, "http://a2a.test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a2a.test") as http:
        client = await ClientFactory(
            ClientConfig(httpx_client=http, supported_protocol_bindings=["HTTP+JSON"], streaming=True)
        ).create_from_url("http://a2a.test")
        initial = message("stream turn")

        async def advance_initial() -> None:
            for _ in range(100):
                context = repository.find_context_by_creation_message_id(initial.message_id)
                if context:
                    await scheduler.run_once(str(context["context_id"]))
                    return
                await asyncio.sleep(0.02)
            raise AssertionError("Task was not admitted")

        wake = asyncio.create_task(advance_initial())
        streamed = [item async for item in client.send_message(a2a.SendMessageRequest(message=initial))]
        await wake
        assert streamed[0].HasField("task")
        assert streamed[0].task.status.state == a2a.TaskState.TASK_STATE_COMPLETED or any(
            item.HasField("message") and item.message.parts[0].text == "Done" for item in streamed
        )
        assert (await client.get_task(a2a.GetTaskRequest(id=streamed[0].task.id))).status.state == (
            a2a.TaskState.TASK_STATE_COMPLETED
        )

    provider.automatic = False
    pending_task = await service.send(message("reconnect", streamed[0].task.context_id))
    await scheduler.run_once(pending_task.context_id)
    restarted = ControllerService(repository, provider, pending, trigger)
    app_after_restart = create_app(restarted, "http://a2a.test")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_after_restart), base_url="http://a2a.test"
    ) as http:
        client = await ClientFactory(
            ClientConfig(httpx_client=http, supported_protocol_bindings=["HTTP+JSON"], streaming=True)
        ).create_from_url("http://a2a.test")

        async def finish_after_snapshot() -> None:
            await asyncio.sleep(0.1)
            context = repository.get_context(pending_task.context_id)
            provider.finish(str(context["cma_session_id"]), "Reconnected")
            await scheduler.run_once(pending_task.context_id)

        finish = asyncio.create_task(finish_after_snapshot())
        observed = [item async for item in client.subscribe(a2a.SubscribeToTaskRequest(id=pending_task.id))]
        await finish
        assert observed[0].HasField("task")
        assert any(
            item.HasField("message") and item.message.parts[0].text == "Reconnected" for item in observed
        )
        assert (await client.get_task(a2a.GetTaskRequest(id=pending_task.id))).status.state == (
            a2a.TaskState.TASK_STATE_COMPLETED
        )


@pytest.mark.asyncio
async def test_stream_emits_complete_agent_message_without_thinking_or_duplicate(
    controller, monkeypatch
) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    watching = watch_started(service, monkeypatch)
    provider.automatic = False
    task = await service.send(message("stream complete message"))
    await scheduler.run_once(task.context_id)
    session_id = str(repository.get_context(task.context_id)["cma_session_id"])
    app = create_app(service, "http://a2a.test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a2a.test") as http:
        client = await ClientFactory(
            ClientConfig(httpx_client=http, supported_protocol_bindings=["HTTP+JSON"], streaming=True)
        ).create_from_url("http://a2a.test")

        async def provide_events() -> None:
            await asyncio.wait_for(watching.wait(), timeout=3)
            provider.emit(
                session_id,
                {"type": "agent.thinking", "content": [{"type": "text", "text": "private"}]},
            )
            provider.emit(
                session_id,
                {"type": "agent.message", "content": [{"type": "text", "text": "Visible"}]},
            )
            await asyncio.sleep(0.1)
            provider.emit(session_id, {"type": "session.status_idle", "stop_reason": {"type": "end_turn"}})
            await scheduler.run_once(task.context_id)

        produce = asyncio.create_task(provide_events())
        observed = [item async for item in client.subscribe(a2a.SubscribeToTaskRequest(id=task.id))]
        await produce
        messages = [item.message for item in observed if item.HasField("message")]
        assert [item.parts[0].text for item in messages] == ["Visible"]
        # SDK 1.1.4 treats a streamed Message as the end of its client iterator.
        assert observed[-1].HasField("message")
        assert (await client.get_task(a2a.GetTaskRequest(id=task.id))).status.state == (
            a2a.TaskState.TASK_STATE_COMPLETED
        )
        again = [item async for item in client.subscribe(a2a.SubscribeToTaskRequest(id=task.id))]
        assert again[0].task.status.state == a2a.TaskState.TASK_STATE_COMPLETED
        assert not any(item.HasField("message") for item in again[1:])


@pytest.mark.asyncio
async def test_raw_http_stream_keeps_terminal_status_after_agent_message(controller, monkeypatch) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    watching = watch_started(service, monkeypatch)
    provider.automatic = False
    task = await service.send(message("raw stream"))
    await scheduler.run_once(task.context_id)
    session_id = str(repository.get_context(task.context_id)["cma_session_id"])
    app = create_app(service, "http://a2a.test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a2a.test") as http:

        async def provide_events() -> None:
            await asyncio.wait_for(watching.wait(), timeout=3)
            provider.emit(
                session_id,
                {"type": "agent.message", "content": [{"type": "text", "text": "On wire"}]},
            )
            await asyncio.sleep(0.1)
            provider.emit(session_id, {"type": "session.status_idle", "stop_reason": {"type": "end_turn"}})
            await scheduler.run_once(task.context_id)

        produce = asyncio.create_task(provide_events())
        response = await http.post(f"/tasks/{task.id}:subscribe", headers={"A2A-Version": "1.0"})
        await produce
        assert response.status_code == 200
        events = [
            Parse(line.removeprefix("data: "), a2a.StreamResponse())
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        assert [event.WhichOneof("payload") for event in events] == [
            "task",
            "message",
            "status_update",
        ]
        assert events[1].message.parts[0].text == "On wire"
        assert events[2].status_update.status.state == a2a.TaskState.TASK_STATE_COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize("history_length, agent_texts", [(0, ["New"]), (1, ["First", "Second"])])
async def test_stream_history_limit_does_not_hide_future_messages(
    controller, monkeypatch, history_length: int, agent_texts: list[str]
) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    watching = watch_started(service, monkeypatch)
    provider.automatic = False
    inbound = message("bounded history")
    app = create_app(service, "http://a2a.test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a2a.test") as http:
        request = a2a.SendMessageRequest(
            message=inbound, configuration=a2a.SendMessageConfiguration(history_length=history_length)
        )

        async def provide_events() -> None:
            for _ in range(100):
                context = repository.find_context_by_creation_message_id(inbound.message_id)
                if context:
                    await scheduler.run_once(str(context["context_id"]))
                    session_id = str(repository.get_context(str(context["context_id"]))["cma_session_id"])
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("Task was not admitted")
            await asyncio.wait_for(watching.wait(), timeout=3)
            for text in agent_texts:
                provider.emit(
                    session_id, {"type": "agent.message", "content": [{"type": "text", "text": text}]}
                )
                await asyncio.sleep(0.05)
            provider.emit(session_id, {"type": "session.status_idle", "stop_reason": {"type": "end_turn"}})
            await scheduler.run_once(str(context["context_id"]))

        produce = asyncio.create_task(provide_events())
        response = await http.post(
            "/message:stream", json=MessageToDict(request), headers={"A2A-Version": "1.0"}
        )
        await produce
        assert response.status_code == 200
        events = [
            Parse(line.removeprefix("data: "), a2a.StreamResponse())
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        assert len(events[0].task.history) == min(history_length, 1)
        assert [event.message.parts[0].text for event in events if event.HasField("message")] == (agent_texts)


def test_context_claim_fencing_and_atomic_fifo(controller) -> None:
    # the controller assigns atomic per-context sequence numbers and fences its single scheduler.
    repository, _provider, _pending, _trigger, _service, _scheduler = controller
    context_id = str(uuid.uuid4())
    context, first, _ = repository.admit(
        proposed_context_id=context_id,
        requested_context_id=None,
        message_id=str(uuid.uuid4()),
        proposed_task_id=str(uuid.uuid4()),
        input_object_key="key-first",
    )
    assert first["sequence"] == 1
    barrier = Barrier(2)

    def admit() -> dict:
        barrier.wait(timeout=3)
        return repository.admit(
            proposed_context_id=str(uuid.uuid4()),
            requested_context_id=context_id,
            message_id=str(uuid.uuid4()),
            proposed_task_id=str(uuid.uuid4()),
            input_object_key=str(uuid.uuid4()),
        )[1]

    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [future.result() for future in [pool.submit(admit), pool.submit(admit)]]
    assert {task["sequence"] for task in tasks} == {2, 3}
    claim = repository.claim_scheduler(context_id)
    assert claim and repository.claim_scheduler(context_id) is None
    with connect(repository.config) as conn:
        conn.execute(
            "UPDATE cma_contexts SET scheduler_lease_expires_at = CURRENT_TIMESTAMP - INTERVAL '1 second' "
            "WHERE context_id = %s",
            (context_id,),
        )
    replacement = repository.claim_scheduler(context_id)
    assert replacement and replacement != claim
    with pytest.raises(LostSchedulerClaim):
        repository.update_context(context_id, claim, active_task_id="stale")
    assert not repository.release_scheduler(context_id, claim)
    assert repository.release_scheduler(context_id, replacement)


def test_initial_message_race_has_one_context_and_task(controller) -> None:
    repository, _provider, _pending, _trigger, _service, _scheduler = controller
    message_id = str(uuid.uuid4())
    barrier = Barrier(2)

    def admit() -> tuple[dict, dict, bool]:
        barrier.wait(timeout=3)
        return repository.admit(
            proposed_context_id=str(uuid.uuid4()),
            requested_context_id=None,
            message_id=message_id,
            proposed_task_id=str(uuid.uuid4()),
            input_object_key=str(uuid.uuid4()),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in [pool.submit(admit), pool.submit(admit)]]
    assert len({result[0]["context_id"] for result in results}) == 1
    assert len({result[1]["task_id"] for result in results}) == 1
    assert sorted(result[2] for result in results) == [False, True]


@pytest.mark.asyncio
async def test_ambiguous_send_is_recovered_without_duplicate(controller) -> None:
    _repository, provider, _pending, _trigger, service, scheduler = controller
    first = await service.send(message("first"))
    await scheduler.run_once(first.context_id)
    provider.lose_send_response = True
    second = await service.send(message("second", first.context_id))
    await scheduler.run_once(first.context_id)
    assert provider.send_calls == 1
    await scheduler.run_once(first.context_id)
    assert provider.send_calls == 1
    assert (await service.get_task(second.id)).status.state == a2a.TaskState.TASK_STATE_COMPLETED


@pytest.mark.asyncio
async def test_ambiguous_create_recovers_and_unproven_send_holds_without_retry(controller) -> None:
    # no provider evidence permits a safe automatic resend of an ambiguous input.
    repository, provider, pending, _trigger, service, scheduler = controller
    provider.lose_create_response = True
    first = await service.send(message("first"))
    await scheduler.run_once(first.context_id)
    assert provider.create_calls == 1
    assert repository.get_task(first.id)["held_reason"] == "ambiguous-create"
    await scheduler.run_once(first.context_id)
    assert provider.create_calls == 1
    assert (await service.get_task(first.id)).status.state == a2a.TaskState.TASK_STATE_COMPLETED

    provider.timeout_before_send_acceptance = True
    second = await service.send(message("uncertain", first.context_id))
    await scheduler.run_once(first.context_id)
    assert provider.send_calls == 1
    await scheduler.run_once(first.context_id)
    assert provider.send_calls == 1
    assert repository.get_task(second.id)["held_reason"] == "ambiguous-dispatch"
    assert (await service.get_task(second.id)).status.state == a2a.TaskState.TASK_STATE_SUBMITTED
    assert len(pending.list_keys()) == 1


@pytest.mark.asyncio
async def test_pending_input_history_survives_restart_and_preacceptance_failure(controller) -> None:
    repository, provider, pending, trigger, service, scheduler = controller
    first = await service.send(message("first"))
    restarted = ControllerService(repository, provider, pending, trigger)
    assert (await restarted.get_task(first.id)).history[0].parts[0].text == "first"
    await scheduler.run_once(first.context_id)
    assert len((await restarted.get_task(first.id)).history) == 2
    assert pending.list_keys() == []

    provider.definitive_send_failure = True
    second = await service.send(message("provider rejects", first.context_id))
    await scheduler.run_once(first.context_id)
    failed = await restarted.get_task(second.id)
    assert failed.status.state == a2a.TaskState.TASK_STATE_FAILED
    assert [item.parts[0].text for item in failed.history] == ["provider rejects"]
    assert len(pending.list_keys()) == 1


@pytest.mark.asyncio
async def test_admin_inspects_and_resolves_only_held_active_tasks(controller) -> None:
    repository, provider, pending, trigger, service, scheduler = controller
    first = await service.send(message("first"))
    await scheduler.run_once(first.context_id)
    admin = ControllerAdmin(repository, trigger)
    with pytest.raises(ValueError, match="not held"):
        admin.inspect(first.id)

    provider.timeout_before_send_acceptance = True
    held = await service.send(message("uncertain", first.context_id))
    next_task = await service.send(message("after held", first.context_id))
    await scheduler.run_once(first.context_id)
    assert admin.inspect(held.id)["reason"] == "ambiguous-send"
    assert any(row["taskId"] == held.id for row in admin.list_held())
    with pytest.raises(ValueError, match="external verification"):
        admin.resolve(held.id, "retry")
    admin.resolve(held.id, "fail")
    assert (await service.get_task(held.id)).status.state == a2a.TaskState.TASK_STATE_FAILED
    assert (await service.get_task(held.id)).history[0].parts[0].text == "uncertain"
    assert len(pending.list_keys()) == 2
    with pytest.raises(ValueError, match="not an active held task"):
        admin.resolve(held.id, "fail")
    await scheduler.run_once(first.context_id)
    await scheduler.run_once(first.context_id)
    assert (await service.get_task(next_task.id)).status.state == a2a.TaskState.TASK_STATE_COMPLETED

    provider.timeout_before_send_acceptance = True
    retried = await service.send(message("verified absent", first.context_id))
    await scheduler.run_once(first.context_id)
    before = provider.send_calls
    admin.resolve(retried.id, "retry", verified_no_side_effect=True)
    assert repository.get_task(retried.id)["internal_state"] == "queued"
    await scheduler.run_once(first.context_id)
    assert provider.send_calls == before + 1
    await scheduler.run_once(first.context_id)
    assert (await service.get_task(retried.id)).status.state == a2a.TaskState.TASK_STATE_COMPLETED
    assert len(pending.list_keys()) == 1


@pytest.mark.asyncio
async def test_admin_inspects_ambiguous_confirmation_and_interrupt(controller) -> None:
    repository, provider, _pending, trigger, service, scheduler = controller
    provider.automatic = False
    admin = ControllerAdmin(repository, trigger)
    task = await service.send(message("approval uncertainty"))
    await scheduler.run_once(task.context_id)
    session_id = str(repository.get_context(task.context_id)["cma_session_id"])
    provider.emit(session_id, {"id": "tool-held", "type": "agent.tool_use", "name": "web_fetch", "input": {}})
    provider.emit(
        session_id,
        {
            "type": "session.status_idle",
            "stop_reason": {"type": "requires_action", "event_ids": ["tool-held"]},
        },
    )
    provider.timeout_before_confirmation_acceptance = True
    await scheduler.run_once(task.context_id)
    assert admin.inspect(task.id)["reason"] == "ambiguous-confirmation"
    admin.resolve(task.id, "fail")
    assert (await service.get_task(task.id)).status.state == a2a.TaskState.TASK_STATE_FAILED

    interrupted = await service.send(message("interrupt uncertainty"))
    await scheduler.run_once(interrupted.context_id)
    await service.cancel(interrupted.id)
    provider.timeout_before_interrupt_acceptance = True
    await scheduler.run_once(interrupted.context_id)
    assert admin.inspect(interrupted.id)["reason"] == "ambiguous-interrupt"
    admin.resolve(interrupted.id, "fail")
    assert (await service.get_task(interrupted.id)).status.state == a2a.TaskState.TASK_STATE_FAILED


@pytest.mark.asyncio
async def test_more_than_one_hundred_provider_events_remain_in_task_history(controller) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    provider.automatic = False
    task = await service.send(message("many events"))
    await scheduler.run_once(task.context_id)
    session_id = str(repository.get_context(task.context_id)["cma_session_id"])
    for index in range(120):
        provider.emit(
            session_id,
            {"type": "agent.message", "content": [{"type": "text", "text": str(index)}]},
        )
    provider.emit(session_id, {"type": "session.status_idle", "stop_reason": {"type": "end_turn"}})
    await scheduler.run_once(task.context_id)
    view = await service.get_task(task.id)
    assert len(view.history) == 121
    assert view.status.message.parts[0].text == "119"
    assert len((await service.get_task(task.id, 1)).history) == 1


@pytest.mark.asyncio
async def test_working_turn_queues_fifo_and_duplicate_wakeups_are_harmless(controller) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    provider.automatic = False
    first = await service.send(message("first working"))
    await scheduler.run_once(first.context_id)
    second = await service.send(message("second queued", first.context_id))
    third = await service.send(message("third queued", first.context_id))
    assert provider.send_calls == 0
    assert [row["task_id"] for row in repository.list_tasks(first.context_id)] == [
        first.id,
        second.id,
        third.id,
    ]
    session_id = str(repository.get_context(first.context_id)["cma_session_id"])
    provider.finish(session_id, "First done")
    await scheduler.run_once(first.context_id)
    await scheduler.run_once(first.context_id)
    assert provider.send_calls == 1
    assert (await service.get_task(third.id)).status.state == a2a.TaskState.TASK_STATE_SUBMITTED
    provider.finish(session_id, "Second done")
    await scheduler.run_once(first.context_id)
    await scheduler.run_once(first.context_id)
    assert provider.send_calls == 2
    provider.finish(session_id, "Third done")
    await scheduler.run_once(first.context_id)
    await scheduler.run_once(first.context_id)
    assert provider.send_calls == 2
    assert (await service.get_task(third.id)).status.state == a2a.TaskState.TASK_STATE_COMPLETED


@pytest.mark.asyncio
async def test_automatic_decisions_and_stale_human_controls(controller) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    provider.automatic = False
    task = await service.send(message("tool turn"))
    await scheduler.run_once(task.context_id)
    session_id = str(repository.get_context(task.context_id)["cma_session_id"])
    emit_calls(provider, session_id, [("tool-a", "web_fetch"), ("tool-b", "unknown")])
    await scheduler.run_once(task.context_id)
    assert provider.confirm_calls == 2
    assert (
        await service.get_task(task.id, options=HUMAN_INPUT_OPTIONS)
    ).status.state == a2a.TaskState.TASK_STATE_WORKING
    assert not (await service.get_task(task.id, options=HUMAN_INPUT_OPTIONS)).status.message.extensions
    assert {r["cma_event_id"]: r["decision"] for r in repository.list_input_requests(task.id)} == {
        "tool-a": "allow",
        "tool-b": "deny",
    }
    for decision in ("allow", "deny"):
        with pytest.raises(InvalidParamsError, match="OpenAPPA review"):
            await service.send(tool_response(task, "tool-a", decision), options=HUMAN_INPUT_OPTIONS)
    await service.cancel(task.id)
    await scheduler.run_once(task.context_id)
    await scheduler.run_once(task.context_id)
    assert (await service.get_task(task.id)).status.state == a2a.TaskState.TASK_STATE_CANCELED
    with pytest.raises(InvalidParamsError, match="OpenAPPA review"):
        await service.send(tool_response(task, "tool-a", "allow"), options=HUMAN_INPUT_OPTIONS)


@pytest.mark.asyncio
async def test_two_surface_tool_responses_cannot_override_policy(controller) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    provider.automatic = False
    task = await service.send(message("shared approval"))
    await scheduler.run_once(task.context_id)
    session_id = str(repository.get_context(task.context_id)["cma_session_id"])
    emit_calls(provider, session_id, [("shared-tool", "web_fetch")])
    await scheduler.run_once(task.context_id)
    outcomes = await asyncio.gather(
        *(
            service.send(tool_response(task, "shared-tool", d), options=HUMAN_INPUT_OPTIONS)
            for d in ("allow", "deny")
        ),
        return_exceptions=True,
    )
    assert all(isinstance(item, InvalidParamsError) for item in outcomes)
    assert repository.list_input_requests(task.id)[0]["decision"] == "allow"
    await scheduler.run_once(task.context_id)
    assert provider.confirm_calls == 1


@pytest.mark.asyncio
async def test_cross_surface_cancel_reconciles_pending_input(controller) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    provider.automatic = False
    scheduler.policy_engine = UnavailablePolicy()
    task = await service.send(message("approval visible in both surfaces"))
    await scheduler.run_once(task.context_id)
    session_id = str(repository.get_context(task.context_id)["cma_session_id"])
    provider.emit(session_id, {"id": "cancel-tool", "type": "agent.tool_use", "name": "browser", "input": {}})
    provider.emit(
        session_id,
        {
            "type": "session.status_idle",
            "stop_reason": {"type": "requires_action", "event_ids": ["cancel-tool"]},
        },
    )
    await scheduler.run_once(task.context_id)
    web_view = await service.get_task(task.id)
    slack_view = await service.get_task(task.id)
    assert web_view.status.state == slack_view.status.state == a2a.TaskState.TASK_STATE_WORKING
    await service.cancel(task.id)
    await scheduler.run_once(task.context_id)
    await scheduler.run_once(task.context_id)
    assert (await service.get_task(task.id)).status.state == a2a.TaskState.TASK_STATE_CANCELED
    assert provider.confirm_calls == 0


@pytest.mark.asyncio
async def test_policy_confirmation_response_loss_recovers_without_second_confirmation(controller) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    provider.automatic = False
    task = await service.send(message("tool turn"))
    await scheduler.run_once(task.context_id)
    session_id = str(repository.get_context(task.context_id)["cma_session_id"])
    emit_calls(provider, session_id, [("tool-loss", "web_fetch")])
    provider.automatic = True
    provider.lose_confirmation_response = True
    await scheduler.run_once(task.context_id)
    await scheduler.run_once(task.context_id)
    assert provider.confirm_calls == 1
    assert repository.list_input_requests(task.id)[0]["status"] == "resolved"
    assert (await service.get_task(task.id)).status.state == a2a.TaskState.TASK_STATE_COMPLETED


@pytest.mark.asyncio
async def test_cancel_denies_approvals_if_interrupt_leaves_session_blocked(controller) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    provider.automatic = False
    scheduler.policy_engine = UnavailablePolicy()
    task = await service.send(message("blocked tool"))
    await scheduler.run_once(task.context_id)
    session_id = str(repository.get_context(task.context_id)["cma_session_id"])
    provider.emit(session_id, {"id": "tool-cancel", "type": "agent.tool_use", "name": "browser", "input": {}})
    provider.emit(
        session_id,
        {
            "type": "session.status_idle",
            "stop_reason": {"type": "requires_action", "event_ids": ["tool-cancel"]},
        },
    )
    await scheduler.run_once(task.context_id)
    provider.interrupt_preserves_approval = True
    provider.lose_interrupt_response = True
    provider.automatic = True
    await service.cancel(task.id)
    await scheduler.run_once(task.context_id)
    await scheduler.run_once(task.context_id)
    await scheduler.run_once(task.context_id)
    assert provider.interrupt_calls == 1
    assert provider.confirm_calls == 1
    assert (await service.get_task(task.id)).status.state == a2a.TaskState.TASK_STATE_CANCELED


@pytest.mark.asyncio
async def test_queued_cancel_has_no_provider_side_effect(controller) -> None:
    _repository, provider, pending, _trigger, service, scheduler = controller
    provider.automatic = False
    first = await service.send(message("slow"))
    await scheduler.run_once(first.context_id)
    queued = await service.send(message("queued", first.context_id))
    app = create_app(service, "http://a2a.test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a2a.test") as http:
        client = await ClientFactory(
            ClientConfig(httpx_client=http, supported_protocol_bindings=["HTTP+JSON"], streaming=False)
        ).create_from_url("http://a2a.test")
        canceled = await client.cancel_task(a2a.CancelTaskRequest(id=queued.id))
    assert canceled.status.state == a2a.TaskState.TASK_STATE_CANCELED
    assert provider.send_calls == 0
    assert len(pending.list_keys()) == 1
    assert [item.parts[0].text for item in canceled.history] == ["queued"]


@pytest.mark.asyncio
async def test_clarification_response_and_cancel_are_controller_owned(controller) -> None:
    repository, provider, pending, _trigger, service, scheduler = controller
    provider.automatic = False
    task = await service.send(message("clarification turn"))
    await scheduler.run_once(task.context_id)
    session_id = str(repository.get_context(task.context_id)["cma_session_id"])
    provider.emit(
        session_id,
        {
            "id": "question-one",
            "type": "agent.custom_tool_use",
            "name": "ask_user",
            "input": {"prompt": "Which environment?", "choices": ["staging", "production"]},
        },
    )
    provider.emit(
        session_id,
        {
            "type": "session.status_idle",
            "stop_reason": {"type": "requires_action", "event_ids": ["question-one"]},
        },
    )
    await scheduler.run_once(task.context_id)
    interrupted = await service.get_task(task.id, options=HUMAN_INPUT_OPTIONS)
    envelope = MessageToDict(interrupted.status.message.metadata, preserving_proto_field_name=True)
    request = envelope[HUMAN_INPUT_EXTENSION_URI]["requests"][0]
    assert request["kind"] == "clarification"
    assert request["prompt"] == "Which environment?"
    assert "question-one" not in str(request)
    response = a2a.Message(
        message_id=str(uuid.uuid4()),
        task_id=task.id,
        context_id=task.context_id,
        role=a2a.Role.ROLE_USER,
        extensions=[HUMAN_INPUT_EXTENSION_URI],
    )
    response.metadata.update(
        {
            HUMAN_INPUT_EXTENSION_URI: {
                "response": {
                    "kind": "clarification",
                    "requestId": request["requestId"],
                    "answer": "staging",
                }
            }
        }
    )
    await service.send(response, options=HUMAN_INPUT_OPTIONS)
    await service.send(response, options=HUMAN_INPUT_OPTIONS)
    await scheduler.run_once(task.context_id)
    assert provider.custom_result_calls == 1
    assert any(
        event["type"] == "user.custom_tool_result" and event["content"][0]["text"] == "staging"
        for event in provider.events[session_id]
    )
    with connect(repository.config) as conn:
        row = conn.execute(
            "SELECT * FROM cma_clarification_requests WHERE task_id = %s", (task.id,)
        ).fetchone()
    assert row["status"] == "resolved"
    assert "staging" not in str(row)
    assert pending.list_keys() == []

    provider.finish(session_id)
    await scheduler.run_once(task.context_id)
    assert (await service.get_task(task.id)).status.state == a2a.TaskState.TASK_STATE_COMPLETED

    provider.interrupt_preserves_approval = True
    provider.automatic = False
    other = await service.send(message("cancel question", task.context_id))
    await scheduler.run_once(task.context_id)
    provider.emit(
        session_id,
        {
            "id": "question-two",
            "type": "agent.custom_tool_use",
            "name": "ask_user",
            "input": {"prompt": "Continue?"},
        },
    )
    provider.emit(
        session_id,
        {
            "type": "session.status_idle",
            "stop_reason": {"type": "requires_action", "event_ids": ["question-two"]},
        },
    )
    await scheduler.run_once(task.context_id)
    await service.cancel(other.id)
    provider.automatic = True
    await scheduler.run_once(task.context_id)
    await scheduler.run_once(task.context_id)
    await scheduler.run_once(task.context_id)
    assert provider.custom_result_calls == 2
    assert any(
        event["type"] == "user.custom_tool_result" and event["is_error"]
        for event in provider.events[session_id]
    )
    assert (await service.get_task(other.id)).status.state == a2a.TaskState.TASK_STATE_CANCELED


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["runtime", "timeout", "publication"])
async def test_policy_operational_failures_do_not_confirm(controller, monkeypatch, caplog, failure) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    provider.automatic = False
    task = await service.send(message("policy failure"))
    await scheduler.run_once(task.context_id)
    session_id = str(repository.get_context(task.context_id)["cma_session_id"])
    emit_calls(provider, session_id, [("first", "web_fetch")])
    await scheduler.run_once(task.context_id)
    assert provider.confirm_calls == 1
    emit_calls(provider, session_id, [("second", "web_search")])

    def fail(*args, **kwargs):
        raise OSError("private diagnostic")

    class TimeoutPolicy:
        async def evaluate_tool_call(self, request, timeout):
            raise TimeoutError("private diagnostic")

    if failure == "runtime":
        scheduler.policy_engine = UnavailablePolicy()
    elif failure == "timeout":
        scheduler.policy_engine = TimeoutPolicy()
    else:
        monkeypatch.setattr(repository, "record_policy_decision", fail)
    await scheduler.run_once(task.context_id)
    assert provider.confirm_calls == 1
    rows = repository.list_input_requests(task.id)
    second = next(r for r in rows if r["cma_event_id"] == "second")
    assert second["status"] == "pending" and second["decision"] is None
    assert second["confirmation_attempted_at"] is None
    assert (await service.get_task(task.id)).status.state == a2a.TaskState.TASK_STATE_WORKING
    assert repository.get_task(task.id)["held_reason"].startswith("policy-")
    assert "private diagnostic" not in caplog.text
    await service.cancel(task.id)
    await scheduler.run_once(task.context_id)
    await scheduler.run_once(task.context_id)
    assert (await service.get_task(task.id)).status.state == a2a.TaskState.TASK_STATE_CANCELED


@pytest.mark.asyncio
async def test_policy_changes_only_affect_new_approval_events(controller, tmp_path, monkeypatch) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    provider.automatic = False
    policy = tmp_path / "appa.toml"
    baseline = Path(scheduler.policy_engine.policy_path).read_text()
    policy.write_text(baseline)
    scheduler.policy_engine = OpenAppaPolicyEngine(scheduler.policy_engine.binary, str(policy))
    task = await service.send(message("policy changes"))
    await scheduler.run_once(task.context_id)
    session_id = str(repository.get_context(task.context_id)["cma_session_id"])
    emit_calls(provider, session_id, [("old-policy", "web_search")])
    publish = repository.record_policy_decision

    def lost_ack(*args, **kwargs):
        publish(*args, **kwargs)
        policy.write_text(baseline.replace("host/cma/web_search", "host/cma/other"))
        raise ConnectionError("publication ack lost")

    monkeypatch.setattr(repository, "record_policy_decision", lost_ack)
    await scheduler.run_once(task.context_id)
    assert provider.confirm_calls == 0
    row = repository.list_input_requests(task.id)[0]
    assert row["status"] == "resolving"
    assert row["decision"] == "allow"  # Never reevaluate an acknowledged or unacknowledged commit.
    monkeypatch.setattr(repository, "record_policy_decision", publish)
    await scheduler.run_once(task.context_id)
    assert provider.confirm_calls == 1
    assert repository.get_task(task.id)["held_reason"] is None
    emit_calls(provider, session_id, [("new-policy", "web_search")])
    await scheduler.run_once(task.context_id)
    await scheduler.run_once(task.context_id)
    assert {r["cma_event_id"]: r["decision"] for r in repository.list_input_requests(task.id)} == {
        "old-policy": "allow",
        "new-policy": "deny",
    }
    assert provider.confirm_calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("crash", ["before", "after"])
async def test_policy_publication_crash_recovery(controller, monkeypatch, crash) -> None:
    repository, provider, _pending, trigger, service, scheduler = controller
    provider.automatic = False
    task = await service.send(message("crash recovery"))
    await scheduler.run_once(task.context_id)
    session_id = str(repository.get_context(task.context_id)["cma_session_id"])
    emit_calls(provider, session_id, [("durable-tool", "web_fetch")])
    publish = repository.record_policy_decision

    class WorkerCrash(BaseException):
        pass

    def crash_publish(*args, **kwargs):
        if crash == "after":
            publish(*args, **kwargs)
        raise WorkerCrash()

    monkeypatch.setattr(repository, "record_policy_decision", crash_publish)
    with pytest.raises(WorkerCrash):
        await scheduler.run_once(task.context_id)
    assert provider.confirm_calls == 0
    assert repository.list_input_requests(task.id)[0]["status"] == (
        "resolving" if crash == "after" else "pending"
    )
    monkeypatch.setattr(repository, "record_policy_decision", publish)
    cold = CmaScheduler(
        repository,
        provider,
        scheduler.pending_input,
        trigger,
        policy_engine=UnavailablePolicy() if crash == "after" else scheduler.policy_engine,
    )
    confirm = provider.confirm_tool

    async def require_publication(*args):
        row = repository.list_input_requests(task.id)[0]
        assert row["status"] == "resolving" and row["decision"] == "allow"
        assert row["confirmation_attempted_at"] is not None
        return await confirm(*args)

    monkeypatch.setattr(provider, "confirm_tool", require_publication)
    await cold.run_once(task.context_id)
    await cold.run_once(task.context_id)
    assert provider.confirm_calls == 1
    assert repository.list_input_requests(task.id)[0]["decision"] == "allow"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        "expired",
        "replaced-claim",
        "inactive",
        "cancelled",
        "missing-request",
        "wrong-task",
        "already-decided",
    ],
)
async def test_policy_publication_fencing_is_atomic(controller, invalid) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    provider.automatic = False
    task = await service.send(message("fenced"))
    await scheduler.run_once(task.context_id)
    claim = repository.claim_scheduler(task.context_id)
    assert claim is not None
    request_id = approval_request_id(task.id, "fenced-call")
    repository.add_input_request(request_id, task.id, "fenced-call", task.context_id, claim)
    if invalid == "expired":
        with connect(repository.config) as conn:
            conn.execute(
                "UPDATE cma_contexts SET scheduler_lease_expires_at = CURRENT_TIMESTAMP - INTERVAL '1 "
                "second' WHERE context_id = %s",
                (task.context_id,),
            )
    elif invalid == "replaced-claim":
        repository.release_scheduler(task.context_id, claim)
        repository.claim_scheduler(task.context_id)
    elif invalid == "inactive":
        repository.update_context(task.context_id, claim, active_task_id=None)
    elif invalid == "cancelled":
        await service.cancel(task.id)
    elif invalid == "already-decided":
        repository.record_policy_decision(task.context_id, task.id, claim, request_id, "deny")
    with pytest.raises(LostSchedulerClaim):
        repository.record_policy_decision(
            task.context_id,
            "wrong" if invalid == "wrong-task" else task.id,
            claim,
            "missing" if invalid == "missing-request" else request_id,
            "allow",
        )
    row = repository.get_input_request(request_id, task.id)
    assert row["decision"] == ("deny" if invalid == "already-decided" else None)
    assert row["status"] == ("resolving" if invalid == "already-decided" else "pending")
    if invalid != "already-decided":
        assert not repository.mark_confirmation_attempted(request_id, task.context_id, claim)
    assert provider.confirm_calls == 0
    repository.release_scheduler(task.context_id, claim)


@pytest.mark.asyncio
async def test_policy_mixed_clarification_and_history_order(controller) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    provider.automatic = False
    task = await service.send(message("mixed"))
    await scheduler.run_once(task.context_id)
    session_id = str(repository.get_context(task.context_id)["cma_session_id"])
    provider.emit(
        session_id,
        {
            "id": "question",
            "type": "agent.custom_tool_use",
            "name": "ask_user",
            "input": {"prompt": "Which environment?"},
        },
    )
    emit_calls(provider, session_id, [("one", "web_fetch"), ("two", "unknown")])
    provider.emit(
        session_id,
        {
            "type": "session.status_idle",
            "stop_reason": {"type": "requires_action", "event_ids": ["two", "question", "one"]},
        },
    )
    seen = []
    native = scheduler.policy_engine

    class RecordingEngine:
        async def evaluate_tool_call(self, request, timeout):
            seen.append(request.invocation_id)
            return await native.evaluate_tool_call(request, timeout)

    scheduler.policy_engine = RecordingEngine()
    await scheduler.run_once(task.context_id)
    view = await service.get_task(task.id, options=HUMAN_INPUT_OPTIONS)
    assert seen == ["one", "two"]
    assert provider.confirm_calls == 2
    assert view.status.state == a2a.TaskState.TASK_STATE_INPUT_REQUIRED
    envelope = MessageToDict(view.status.message.metadata)[HUMAN_INPUT_EXTENSION_URI]
    assert [r["kind"] for r in envelope["requests"]] == ["clarification"]
    await scheduler.run_once(task.context_id)
    assert provider.confirm_calls == 2 and seen == ["one", "two"]


@pytest.mark.asyncio
async def test_cancellation_during_native_evaluation_cannot_publish_allow(controller) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    provider.automatic = False
    task = await service.send(message("cancel during evaluation"))
    await scheduler.run_once(task.context_id)
    session_id = str(repository.get_context(task.context_id)["cma_session_id"])
    emit_calls(provider, session_id, [("cancel-race", "web_fetch")])
    native = scheduler.policy_engine

    class CancellingEngine:
        async def evaluate_tool_call(self, request, timeout):
            result = await native.evaluate_tool_call(request, timeout)
            await service.cancel(task.id)
            return result

    scheduler.policy_engine = CancellingEngine()
    with pytest.raises(LostSchedulerClaim):
        await scheduler.run_once(task.context_id)
    assert provider.confirm_calls == 0
    row = repository.list_input_requests(task.id)[0]
    assert row["status"] == "pending" and row["decision"] is None
    scheduler.policy_engine = UnavailablePolicy()
    provider.interrupt_preserves_approval = True
    await scheduler.run_once(task.context_id)
    await scheduler.run_once(task.context_id)
    assert provider.confirm_calls == 1
    assert repository.list_input_requests(task.id)[0]["decision"] == "deny"


@pytest.mark.asyncio
async def test_unsupported_remedy_never_publishes_a_decision_or_human_controls(controller) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    provider.automatic = False
    task = await service.send(message("unsupported remedy"))
    await scheduler.run_once(task.context_id)
    session_id = str(repository.get_context(task.context_id)["cma_session_id"])
    emit_calls(provider, session_id, [("ask-tool", "web_fetch")])
    native = scheduler.policy_engine

    class RemedyEngine:
        async def evaluate_tool_call(self, request, timeout):
            await native.evaluate_tool_call(request, timeout)
            raise PolicyError("policy-remedy-unsupported")

    scheduler.policy_engine = RemedyEngine()
    await scheduler.run_once(task.context_id)
    row = repository.list_input_requests(task.id)[0]
    assert row["decision"] is None and row["confirmation_attempted_at"] is None
    claim = repository.claim_scheduler(task.context_id)
    with pytest.raises(ValueError):
        repository.record_policy_decision(task.context_id, task.id, claim, row["request_id"], "ask")
    repository.release_scheduler(task.context_id, claim)
    assert repository.list_input_requests(task.id)[0]["status"] == "pending"
    assert repository.get_task(task.id)["held_reason"] == "policy-remedy-unsupported"
    assert (
        await service.get_task(task.id, options=HUMAN_INPUT_OPTIONS)
    ).status.state == a2a.TaskState.TASK_STATE_WORKING
    assert provider.confirm_calls == 0


@pytest.mark.asyncio
async def test_worker_loses_lease_during_native_evaluation(controller) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    provider.automatic = False
    task = await service.send(message("worker expired"))
    await scheduler.run_once(task.context_id)
    session_id = str(repository.get_context(task.context_id)["cma_session_id"])
    emit_calls(provider, session_id, [("expired-call", "web_fetch")])
    native = scheduler.policy_engine

    class ExpiringEngine:
        async def evaluate_tool_call(self, request, timeout):
            from managed_agents_app.cma_controller.scheduler import SchedulerBusy

            with pytest.raises(SchedulerBusy):
                await scheduler.run_once(task.context_id)
            decision = await native.evaluate_tool_call(request, timeout)
            with connect(repository.config) as conn:
                conn.execute(
                    "UPDATE cma_contexts SET scheduler_lease_expires_at = CURRENT_TIMESTAMP - "
                    "INTERVAL '1 second' WHERE context_id = %s",
                    (task.context_id,),
                )
            return decision

    scheduler.policy_engine = ExpiringEngine()
    with pytest.raises(LostSchedulerClaim):
        await scheduler.run_once(task.context_id)
    assert provider.confirm_calls == 0
    assert repository.list_input_requests(task.id)[0]["decision"] is None
    scheduler.policy_engine = native
    await scheduler.run_once(task.context_id)
    assert provider.confirm_calls == 1


@pytest.mark.asyncio
async def test_missing_decision_cannot_confirm(controller) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    provider.automatic = False
    scheduler.policy_engine = UnavailablePolicy()
    task = await service.send(message("missing decision"))
    await scheduler.run_once(task.context_id)
    session_id = str(repository.get_context(task.context_id)["cma_session_id"])
    emit_calls(provider, session_id, [("unpublished-tool", "web_fetch")])
    await scheduler.run_once(task.context_id)
    with connect(repository.config) as conn:
        conn.execute("UPDATE cma_input_requests SET status = 'resolving' WHERE task_id = %s", (task.id,))
    await scheduler.run_once(task.context_id)
    assert provider.confirm_calls == 0
    assert repository.get_task(task.id)["held_reason"] == "policy-unpublished-decision"


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["expired", "inactive", "cancelled", "missing-decision"])
async def test_confirmation_checkpoint_fences_active_task_lease_and_cancel(controller, guard) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    provider.automatic = False
    task = await service.send(message("confirmation guard"))
    await scheduler.run_once(task.context_id)
    claim = repository.claim_scheduler(task.context_id)
    request_id = approval_request_id(task.id, "guarded")
    repository.add_input_request(request_id, task.id, "guarded", task.context_id, claim)
    repository.record_policy_decision(task.context_id, task.id, claim, request_id, "allow")
    with connect(repository.config) as conn:
        if guard == "expired":
            conn.execute(
                "UPDATE cma_contexts SET scheduler_lease_expires_at = CURRENT_TIMESTAMP - INTERVAL '1 "
                "second' WHERE context_id = %s",
                (task.context_id,),
            )
        elif guard == "inactive":
            conn.execute(
                "UPDATE cma_contexts SET active_task_id = NULL WHERE context_id = %s", (task.context_id,)
            )
        elif guard == "cancelled":
            conn.execute(
                "UPDATE cma_tasks SET cancel_requested_at = CURRENT_TIMESTAMP WHERE task_id = %s", (task.id,)
            )
        else:
            conn.execute("UPDATE cma_input_requests SET decision = NULL WHERE request_id = %s", (request_id,))
    assert not repository.mark_confirmation_attempted(request_id, task.context_id, claim)
    assert repository.get_input_request(request_id, task.id)["confirmation_attempted_at"] is None
    if guard == "cancelled":
        repository.deny_cancelled_input_request(request_id, task.context_id, claim)
        assert repository.mark_confirmation_attempted(request_id, task.context_id, claim)
        assert not repository.mark_confirmation_attempted(request_id, task.context_id, claim)
    repository.release_scheduler(task.context_id, claim)


@pytest.mark.asyncio
async def test_policy_defers_additional_calls_to_remaining_lease(controller) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    provider.automatic = False
    task = await service.send(message("bounded calls"))
    await scheduler.run_once(task.context_id)
    session_id = str(repository.get_context(task.context_id)["cma_session_id"])
    emit_calls(provider, session_id, [("first", "web_fetch"), ("second", "web_search")])
    native = scheduler.policy_engine

    class BoundedEngine:
        async def evaluate_tool_call(self, request, timeout):
            decision = await native.evaluate_tool_call(request, timeout)
            with connect(repository.config) as conn:
                conn.execute(
                    "UPDATE cma_contexts SET scheduler_lease_expires_at = CURRENT_TIMESTAMP + "
                    "INTERVAL '5 second' WHERE context_id = %s",
                    (task.context_id,),
                )
            return decision

    scheduler.policy_engine = BoundedEngine()
    await scheduler.run_once(task.context_id)
    assert provider.confirm_calls == 1
    assert repository.list_input_requests(task.id)[0]["decision"] == "allow"
    assert (
        next(r for r in repository.list_input_requests(task.id) if r["cma_event_id"] == "second")["status"]
        == "pending"
    )
    scheduler.policy_engine = native
    await scheduler.run_once(task.context_id)
    assert provider.confirm_calls == 2
    assert all(row["decision"] == "allow" for row in repository.list_input_requests(task.id))


@pytest.mark.asyncio
async def test_policy_normalizes_provider_mcp_identity(controller) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    provider.automatic = False
    scheduler.policy_engine = OpenAppaPolicyEngine(
        str(Path(__file__).resolve().parents[3] / ".generated/openappa-bin/openappa-cma"),
        str(Path(__file__).resolve().parents[1] / "fixtures/openappa-mcp.toml"),
    )
    task = await service.send(message("mcp fixture"))
    await scheduler.run_once(task.context_id)
    session_id = str(repository.get_context(task.context_id)["cma_session_id"])
    for event_id, name in [("mcp-allow", "lookup"), ("mcp-deny", "unknown")]:
        provider.emit(
            session_id,
            {
                "id": event_id,
                "type": "agent.mcp_tool_use",
                "mcp_server_name": "fixture",
                "name": name,
                "input": {},
            },
        )
    provider.emit(
        session_id,
        {
            "type": "session.status_idle",
            "stop_reason": {"type": "requires_action", "event_ids": ["mcp-deny", "mcp-allow"]},
        },
    )
    await scheduler.run_once(task.context_id)
    assert provider.confirm_calls == 2
    assert {r["cma_event_id"]: r["decision"] for r in repository.list_input_requests(task.id)} == {
        "mcp-allow": "allow",
        "mcp-deny": "deny",
    }


@pytest.mark.asyncio
async def test_scheduled_reconciliation_finishes_without_webhook_or_sse(controller):
    repository, provider, _pending, trigger, service, scheduler = controller
    provider.automatic = False
    current = await service.send(message("lost webhook"))
    trigger.pending.clear()
    await scheduler.run_once(current.context_id)
    assert trigger.pending == [(current.context_id, "reconcile-active", 30)]
    context_id, reason, delay = trigger.pending.pop()
    session_id = str(repository.get_context(context_id)["cma_session_id"])
    provider.finish(session_id, "Recovered from history")
    # Deliver the scheduled wakeup deterministically; no webhook or wall-clock wait.
    assert reason == "reconcile-active" and delay == 30
    await scheduler.run_once(context_id)
    final = await service.get_task(current.id)
    assert final.status.state == a2a.TaskState.TASK_STATE_COMPLETED
    assert final.history[-1].parts[0].text == "Recovered from history"
    assert provider.create_calls == 1 and provider.send_calls == 0
    assert trigger.pending == []


@pytest.mark.asyncio
async def test_competing_policy_publications_commit_only_one_decision(controller) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    provider.automatic = False
    task = await service.send(message("competing publications"))
    await scheduler.run_once(task.context_id)
    claim = repository.claim_scheduler(task.context_id)
    request_id = approval_request_id(task.id, "contested")
    repository.add_input_request(request_id, task.id, "contested", task.context_id, claim)
    barrier = Barrier(2)

    def publish(decision):
        barrier.wait(timeout=5)
        try:
            repository.record_policy_decision(task.context_id, task.id, claim, request_id, decision)
            return decision
        except LostSchedulerClaim:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(publish, decision) for decision in ("allow", "deny")]
        outcomes = [future.result(timeout=10) for future in futures]
    committed = [decision for decision in outcomes if decision is not None]
    assert len(committed) == 1
    row = repository.get_input_request(request_id, task.id)
    assert row["status"] == "resolving" and row["decision"] == committed[0]
    assert row["confirmation_attempted_at"] is None and provider.confirm_calls == 0
    repository.release_scheduler(task.context_id, claim)


def review_policy(scheduler, tmp_path):
    path = tmp_path / "review.toml"
    path.write_text(
        (Path(__file__).resolve().parents[3] / "services/openappa/tests/human-review.toml").read_text()
    )
    scheduler.policy_engine = OpenAppaPolicyEngine(scheduler.policy_engine.binary, str(path))
    return path


async def pending_review(controller, tmp_path):
    repository, provider, _, _, service, scheduler = controller
    provider.automatic = False
    path = review_policy(scheduler, tmp_path)
    task = await service.send(message("native human review"))
    await scheduler.run_once(task.context_id)
    session = str(repository.get_context(task.context_id)["cma_session_id"])
    provider.emit(
        session,
        {
            "id": "review-tool",
            "type": "agent.tool_use",
            "name": "web_search",
            "input": {"query": "private review argument"},
        },
    )
    provider.emit(
        session,
        {
            "type": "session.status_idle",
            "stop_reason": {"type": "requires_action", "event_ids": ["review-tool"]},
        },
    )
    await scheduler.run_once(task.context_id)
    row = repository.list_input_requests(task.id)[0]
    assert row["review_digest"] and row["human_ruling"] is None and row["decision"] is None
    assert provider.confirm_calls == 0
    view = await service.get_task(task.id, options=HUMAN_INPUT_OPTIONS)
    assert view.status.state == a2a.TaskState.TASK_STATE_INPUT_REQUIRED
    request = MessageToDict(view.status.message.metadata)[HUMAN_INPUT_EXTENSION_URI]["requests"][0]
    assert request["requestId"] == row["request_id"]
    assert request["tool"]["arguments"] == {"query": "private review argument"}
    assert "user-approval" in request["message"]
    assert "private review argument" not in str(row)
    response = tool_response(task, "unused", "allow")
    response.metadata[HUMAN_INPUT_EXTENSION_URI]["response"]["requestId"] = row["request_id"]
    return task, row, response, path, session


@pytest.mark.asyncio
@pytest.mark.parametrize("ruling", ["allow", "deny"])
async def test_native_human_review_first_ruling_and_fenced_confirmation(
    controller, tmp_path, monkeypatch, ruling
):
    repository, provider, _, _, service, scheduler = controller
    task, row, response, _, _ = await pending_review(controller, tmp_path)
    response.metadata[HUMAN_INPUT_EXTENSION_URI]["response"]["decision"] = ruling
    await service.send(response, options=HUMAN_INPUT_OPTIONS)
    await service.send(response, options=HUMAN_INPUT_OPTIONS)
    assert provider.confirm_calls == 0
    assert repository.get_input_request(row["request_id"], task.id)["human_ruling"] == ruling
    conflict = a2a.Message()
    conflict.CopyFrom(response)
    conflict.metadata[HUMAN_INPUT_EXTENSION_URI]["response"]["decision"] = (
        "deny" if ruling == "allow" else "allow"
    )
    with pytest.raises(InvalidParamsError):
        await service.send(conflict, options=HUMAN_INPUT_OPTIONS)
    confirm = provider.confirm_tool

    async def fenced(*args):
        saved = repository.get_input_request(row["request_id"], task.id)
        assert saved["status"] == "resolving" and saved["decision"] == ruling
        assert saved["confirmation_attempted_at"] is not None
        return await confirm(*args)

    monkeypatch.setattr(provider, "confirm_tool", fenced)
    provider.lose_confirmation_response = True
    await scheduler.run_once(task.context_id)
    await scheduler.run_once(task.context_id)
    assert provider.confirm_calls == 1
    assert repository.get_input_request(row["request_id"], task.id)["status"] == "resolved"


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["policy", "arguments"])
async def test_changed_native_review_rejects_old_consent(controller, tmp_path, changed):
    repository, provider, _, _, service, scheduler = controller
    task, row, response, path, session = await pending_review(controller, tmp_path)
    await service.send(response, options=HUMAN_INPUT_OPTIONS)
    if changed == "policy":
        path.write_text(path.read_text() + "\n# new policy revision")
    else:
        event = next(e for e in provider.events[session] if e.get("id") == "review-tool")
        event["input"] = {"query": "changed"}
    await scheduler.run_once(task.context_id)
    await scheduler.run_once(task.context_id)
    fresh = repository.list_input_requests(task.id)[0]
    assert fresh["request_id"] != row["request_id"] and fresh["review_digest"] != row["review_digest"]
    assert fresh["human_ruling"] is None and fresh["decision"] is None and provider.confirm_calls == 0
    with pytest.raises(InvalidParamsError):
        await service.send(response, options=HUMAN_INPUT_OPTIONS)


@pytest.mark.asyncio
async def test_cancel_during_native_human_resolution_cannot_allow(controller, tmp_path):
    repository, provider, _, _, service, scheduler = controller
    task, row, response, _, _ = await pending_review(controller, tmp_path)
    await service.send(response, options=HUMAN_INPUT_OPTIONS)
    native = scheduler.policy_engine

    class CancelDuringResolution:
        async def resolve_tool_call(self, *args):
            result = await native.resolve_tool_call(*args)
            await service.cancel(task.id)
            return result

    scheduler.policy_engine = CancelDuringResolution()
    with pytest.raises(LostSchedulerClaim):
        await scheduler.run_once(task.context_id)
    assert provider.confirm_calls == 0
    provider.interrupt_preserves_approval = True
    scheduler.policy_engine = UnavailablePolicy()
    await scheduler.run_once(task.context_id)
    await scheduler.run_once(task.context_id)
    assert provider.confirm_calls == 1
    assert repository.get_input_request(row["request_id"], task.id)["decision"] == "deny"
    with pytest.raises(InvalidParamsError):
        await service.send(response, options=HUMAN_INPUT_OPTIONS)


@pytest.mark.asyncio
async def test_concurrent_human_rulings_have_one_winner(controller, tmp_path):
    repository, provider, _, _, _, _ = controller
    task, row, _, _, _ = await pending_review(controller, tmp_path)
    barrier = Barrier(2)

    def decide(ruling):
        barrier.wait()
        return repository.record_human_ruling(row["request_id"], task.id, ruling)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(decide, ["allow", "deny"]))
    assert sorted(results) == [False, True]
    assert provider.confirm_calls == 0


@pytest.mark.asyncio
async def test_review_outage_retires_controls_and_recovers_with_new_request(controller, tmp_path):
    repository, provider, _, _, service, scheduler = controller
    task, row, response, _, _ = await pending_review(controller, tmp_path)
    native = scheduler.policy_engine
    scheduler.policy_engine = UnavailablePolicy()
    await scheduler.run_once(task.context_id)
    held = await service.get_task(task.id, options=HUMAN_INPUT_OPTIONS)
    assert held.status.state == a2a.TaskState.TASK_STATE_WORKING
    assert not held.status.message.metadata
    with pytest.raises(InvalidParamsError):
        await service.send(response, options=HUMAN_INPUT_OPTIONS)
    scheduler.policy_engine = native
    await scheduler.run_once(task.context_id)
    fresh = repository.list_input_requests(task.id)[0]
    assert fresh["request_id"] != row["request_id"] and fresh["human_ruling"] is None
    assert provider.confirm_calls == 0


@pytest.mark.asyncio
async def test_preview_hint_is_transition_only_after_commit_and_failure_is_disposable(controller) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    provider.automatic = False
    hints = []

    class Preview:
        def start(self, hint):
            context = repository.get_context(hint.context_id)
            assert context["scheduler_claim_id"] is None
            assert repository.get_task(hint.task_id)["cma_input_event_id"] is not None
            hints.append(hint)
            raise TimeoutError("preview queue unavailable")

    scheduler.preview_trigger = Preview()
    task = await service.send(message("preview outage"))
    await scheduler.run_once(task.context_id)
    await scheduler.run_once(task.context_id)
    assert len(hints) == 1
    assert provider.create_calls == 1 and provider.send_calls == 0
    assert (await service.get_task(task.id)).status.state == a2a.TaskState.TASK_STATE_WORKING
    provider.finish(hints[0].session_id, "Canonical despite preview outage")
    await scheduler.run_once(task.context_id)
    assert (await service.get_task(task.id)).status.state == a2a.TaskState.TASK_STATE_COMPLETED
    assert len(hints) == 1


@pytest.mark.asyncio
async def test_preview_resumes_after_confirm_without_duplicate_confirmation(controller) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    provider.automatic = False
    hints = []

    class Preview:
        def start(self, hint):
            hints.append(hint)

    scheduler.preview_trigger = Preview()
    task = await service.send(message("confirm then preview"))
    await scheduler.run_once(task.context_id)
    session = str(repository.get_context(task.context_id)["cma_session_id"])
    emit_calls(provider, session, [("approved-call", "web_fetch")])
    await scheduler.run_once(task.context_id)
    await scheduler.run_once(task.context_id)
    assert len(hints) == 2
    assert hints[0].phase_id != hints[1].phase_id
    assert provider.confirm_calls == 1 and provider.create_calls == 1
    assert (await service.get_task(task.id)).status.state == a2a.TaskState.TASK_STATE_WORKING
