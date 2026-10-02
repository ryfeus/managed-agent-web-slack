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
    pending = FilePendingInputStore(tmp_path)
    trigger = LocalSchedulerTrigger()
    service = ControllerService(repository, provider, pending, trigger)
    scheduler = CmaScheduler(repository, provider, pending, trigger)
    return repository, provider, pending, trigger, service, scheduler


def message(text: str, context_id: str = "", message_id: str | None = None) -> a2a.Message:
    return a2a.Message(
        message_id=message_id or str(uuid.uuid4()),
        context_id=context_id,
        role=a2a.Role.ROLE_USER,
        parts=[a2a.Part(text=text)],
    )


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
                            {"id": "blocking-tool", "type": "agent.tool_use", "name": "browser", "input": {}},
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
        session_id, {"id": "negotiated-tool", "type": "agent.tool_use", "name": "browser", "input": {}}
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
                        "kind": "tool_approval",
                        "requestId": approval_request_id(task.id, "negotiated-tool"),
                        "decision": "allow",
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
async def test_stream_emits_complete_agent_message_without_thinking_or_duplicate(controller) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
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
            for _ in range(100):
                if provider.listeners.get(session_id):
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("Provider stream was not subscribed")
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
        # KL-004: SDK 1.1.4 treats a streamed Message as the end of its client iterator.
        assert observed[-1].HasField("message")
        assert (await client.get_task(a2a.GetTaskRequest(id=task.id))).status.state == (
            a2a.TaskState.TASK_STATE_COMPLETED
        )
        again = [item async for item in client.subscribe(a2a.SubscribeToTaskRequest(id=task.id))]
        assert again[0].task.status.state == a2a.TaskState.TASK_STATE_COMPLETED
        assert not any(item.HasField("message") for item in again[1:])


@pytest.mark.asyncio
async def test_raw_http_stream_keeps_terminal_status_after_agent_message(controller) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    provider.automatic = False
    task = await service.send(message("raw stream"))
    await scheduler.run_once(task.context_id)
    session_id = str(repository.get_context(task.context_id)["cma_session_id"])
    app = create_app(service, "http://a2a.test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a2a.test") as http:

        async def provide_events() -> None:
            for _ in range(100):
                if provider.listeners.get(session_id):
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("Provider stream was not subscribed")
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
    controller, history_length: int, agent_texts: list[str]
) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
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
            for _ in range(100):
                if provider.listeners.get(session_id):
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("Provider stream was not subscribed")
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
    # KL-001: the controller assigns atomic per-context sequence numbers and fences its single scheduler.
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
    # KL-003: no provider evidence permits a safe automatic resend of an ambiguous input.
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
    provider.emit(session_id, {"id": "tool-held", "type": "agent.tool_use", "name": "browser", "input": {}})
    provider.emit(
        session_id,
        {
            "type": "session.status_idle",
            "stop_reason": {"type": "requires_action", "event_ids": ["tool-held"]},
        },
    )
    await scheduler.run_once(task.context_id)
    approval = a2a.Message(
        message_id=str(uuid.uuid4()),
        context_id=task.context_id,
        task_id=task.id,
        role=a2a.Role.ROLE_USER,
        extensions=[HUMAN_INPUT_EXTENSION_URI],
    )
    approval.metadata.update(
        {
            HUMAN_INPUT_EXTENSION_URI: {
                "response": {
                    "kind": "tool_approval",
                    "requestId": approval_request_id(task.id, "tool-held"),
                    "decision": "allow",
                }
            }
        }
    )
    await service.send(approval, options=HUMAN_INPUT_OPTIONS)
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
async def test_approval_rejection_and_cancellation(controller) -> None:
    repository, provider, pending, _trigger, service, scheduler = controller
    provider.automatic = False
    first = await service.send(message("tool turn"))
    await scheduler.run_once(first.context_id)
    session_id = str(repository.get_context(first.context_id)["cma_session_id"])
    provider.emit(session_id, {"id": "tool-a", "type": "agent.tool_use", "name": "browser", "input": {}})
    provider.emit(session_id, {"id": "tool-b", "type": "agent.tool_use", "name": "bash", "input": {}})
    provider.emit(
        session_id,
        {
            "type": "session.status_idle",
            "stop_reason": {"type": "requires_action", "event_ids": ["tool-a", "tool-b"]},
        },
    )
    await scheduler.run_once(first.context_id)
    waiting = await service.get_task(first.id)
    assert waiting.status.state == a2a.TaskState.TASK_STATE_INPUT_REQUIRED
    rejected = await service.send(message("new turn", first.context_id))
    assert rejected.status.state == a2a.TaskState.TASK_STATE_REJECTED
    assert [item.parts[0].text for item in rejected.history] == ["new turn"]
    assert len(pending.list_keys()) == 1
    approval = a2a.Message(
        message_id=str(uuid.uuid4()),
        task_id=first.id,
        context_id=first.context_id,
        role=a2a.Role.ROLE_USER,
        extensions=["https://ryfeus.github.io/claude-managed-agents-ui-eda/a2a/extensions/human-input/v1"],
    )
    uri = approval.extensions[0]
    approval.metadata.update(
        {
            uri: {
                "response": {
                    "kind": "tool_approval",
                    "requestId": approval_request_id(first.id, "tool-a"),
                    "decision": "allow",
                }
            }
        }
    )
    app = create_app(service, "http://a2a.test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://a2a.test") as http:
        client = await ClientFactory(
            ClientConfig(httpx_client=http, supported_protocol_bindings=["HTTP+JSON"], streaming=False)
        ).create_from_url("http://a2a.test")
        continuation = a2a.SendMessageRequest(
            message=approval, configuration=a2a.SendMessageConfiguration(return_immediately=True)
        )
        assert [item async for item in client.send_message(continuation, context=HUMAN_INPUT_CALL)][
            0
        ].task.id == first.id
        assert [item async for item in client.send_message(continuation, context=HUMAN_INPUT_CALL)][
            0
        ].task.id == first.id
    await scheduler.run_once(first.context_id)
    assert provider.confirm_calls == 1
    requests = repository.list_input_requests(first.id)
    assert {row["request_id"]: row["status"] for row in requests} == {
        approval_request_id(first.id, "tool-a"): "resolved",
        approval_request_id(first.id, "tool-b"): "pending",
    }
    with pytest.raises(Exception, match="different decision"):
        approval.metadata.update(
            {
                uri: {
                    "response": {
                        "kind": "tool_approval",
                        "requestId": approval_request_id(first.id, "tool-a"),
                        "decision": "deny",
                    }
                }
            }
        )
        await service.send(approval, options=HUMAN_INPUT_OPTIONS)
    await service.cancel(first.id)
    await scheduler.run_once(first.context_id)
    await scheduler.run_once(first.context_id)
    assert (await service.get_task(first.id)).status.state == a2a.TaskState.TASK_STATE_CANCELED
    approval.metadata.update(
        {
            uri: {
                "response": {
                    "kind": "tool_approval",
                    "requestId": approval_request_id(first.id, "tool-a"),
                    "decision": "allow",
                }
            }
        }
    )
    assert (await service.send(approval, options=HUMAN_INPUT_OPTIONS)).status.state == (
        a2a.TaskState.TASK_STATE_CANCELED
    )


@pytest.mark.asyncio
async def test_two_surface_responses_race_on_one_controller_request(controller) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    provider.automatic = False
    task = await service.send(message("shared approval"))
    await scheduler.run_once(task.context_id)
    session_id = str(repository.get_context(task.context_id)["cma_session_id"])
    provider.emit(session_id, {"id": "shared-tool", "type": "agent.tool_use", "name": "browser", "input": {}})
    provider.emit(
        session_id,
        {
            "type": "session.status_idle",
            "stop_reason": {"type": "requires_action", "event_ids": ["shared-tool"]},
        },
    )
    await scheduler.run_once(task.context_id)
    assert (await service.get_task(task.id)).status.state == a2a.TaskState.TASK_STATE_INPUT_REQUIRED
    request_id = approval_request_id(task.id, "shared-tool")

    def response(decision: str) -> a2a.Message:
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
                    "response": {"kind": "tool_approval", "requestId": request_id, "decision": decision}
                }
            }
        )
        return item

    web, slack = response("allow"), response("deny")
    outcomes = await asyncio.gather(
        service.send(web, options=HUMAN_INPUT_OPTIONS),
        service.send(slack, options=HUMAN_INPUT_OPTIONS),
        return_exceptions=True,
    )
    winners = [item for item in outcomes if isinstance(item, a2a.Task)]
    losers = [item for item in outcomes if isinstance(item, InvalidParamsError)]
    assert len(winners) == len(losers) == 1
    assert winners[0].id == task.id
    assert "different decision" in str(losers[0])
    stored = repository.list_input_requests(task.id)[0]
    winning = web if stored["decision"] == "allow" else slack
    conflicting = slack if winning is web else web
    assert (await service.send(winning, options=HUMAN_INPUT_OPTIONS)).id == task.id
    with pytest.raises(InvalidParamsError, match="different decision"):
        await service.send(conflicting, options=HUMAN_INPUT_OPTIONS)
    await scheduler.run_once(task.context_id)
    assert provider.confirm_calls == 1


@pytest.mark.asyncio
async def test_cross_surface_cancel_reconciles_pending_input(controller) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    provider.automatic = False
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
    assert web_view.status.state == slack_view.status.state == a2a.TaskState.TASK_STATE_INPUT_REQUIRED
    await service.cancel(task.id)
    await scheduler.run_once(task.context_id)
    await scheduler.run_once(task.context_id)
    assert (await service.get_task(task.id)).status.state == a2a.TaskState.TASK_STATE_CANCELED
    assert provider.confirm_calls == 0


@pytest.mark.asyncio
async def test_approval_confirmation_response_loss_recovers_without_second_confirmation(controller) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    provider.automatic = False
    task = await service.send(message("approval turn"))
    await scheduler.run_once(task.context_id)
    session_id = str(repository.get_context(task.context_id)["cma_session_id"])
    provider.emit(session_id, {"id": "tool-loss", "type": "agent.tool_use", "name": "browser", "input": {}})
    provider.emit(
        session_id,
        {
            "type": "session.status_idle",
            "stop_reason": {"type": "requires_action", "event_ids": ["tool-loss"]},
        },
    )
    await scheduler.run_once(task.context_id)
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
                    "kind": "tool_approval",
                    "requestId": approval_request_id(task.id, "tool-loss"),
                    "decision": "allow",
                }
            }
        }
    )
    await service.send(approval, options=HUMAN_INPUT_OPTIONS)
    provider.automatic = True
    provider.lose_confirmation_response = True
    await scheduler.run_once(task.context_id)
    await scheduler.run_once(task.context_id)
    assert provider.confirm_calls == 1
    assert repository.list_input_requests(task.id)[0]["status"] == "resolved"
    assert (await service.get_task(task.id)).status.state == a2a.TaskState.TASK_STATE_COMPLETED
    assert (await service.send(approval, options=HUMAN_INPUT_OPTIONS)).id == task.id


@pytest.mark.asyncio
async def test_cancel_denies_approvals_if_interrupt_leaves_session_blocked(controller) -> None:
    repository, provider, _pending, _trigger, service, scheduler = controller
    provider.automatic = False
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
