from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path

import httpx
import pytest
from a2a.client.client import ClientConfig
from a2a.client.client_factory import ClientFactory
from a2a.types import a2a_pb2 as a2a
from a2a.utils.errors import InvalidParamsError, TaskNotFoundError
from google.protobuf.json_format import MessageToJson  # type: ignore[import-untyped]

from managed_agents_app.a2a_client.client import A2AControllerClient, DefinitiveA2ARequestError
from managed_agents_app.a2a_event_sink.app import create_app as create_sink
from managed_agents_app.a2a_event_sink.repository import EventSinkRepository
from managed_agents_app.agent_control_plane.service import ThreadAgentService
from managed_agents_app.agents.registry import AgentRegistration, AgentRegistry
from managed_agents_app.cma_controller import app as controller_app
from managed_agents_app.cma_controller.admin import ControllerAdmin
from managed_agents_app.cma_controller.app import create_app as create_controller
from managed_agents_app.cma_controller.composition import build_controller
from managed_agents_app.cma_controller.pending_input import FilePendingInputStore
from managed_agents_app.cma_controller.push_dispatcher import PushDispatcher
from managed_agents_app.cma_controller.push_repository import PushRepository
from managed_agents_app.cma_controller.push_trigger import AsyncLocalPushTrigger
from managed_agents_app.cma_controller.runtime_repository import ControllerRuntimeRepository
from managed_agents_app.cma_controller.trigger import LocalSchedulerTrigger
from managed_agents_app.config import load_config
from managed_agents_app.db import Database
from managed_agents_app.db.connection import connect
from managed_agents_app.db.thread_repository import ThreadRepository
from managed_agents_app.testing.factory import validate_local_database
from managed_agents_app.testing.fake_cma_provider import FakeCmaProvider
from managed_agents_app.testing.local_event_bus import LocalEventBus

CONTROLLER_URL = "http://a2a.test"
SINK_URL = "http://a2a-event-sink.test/internal/a2a/events/cma"


@pytest.fixture
def components(tmp_path: Path):
    config = load_config()
    validate_local_database(config)
    runtime = ControllerRuntimeRepository(config)
    provider = FakeCmaProvider()
    pending = FilePendingInputStore(tmp_path)
    scheduler_trigger = LocalSchedulerTrigger()
    push_trigger = AsyncLocalPushTrigger()
    composition = build_controller(
        config.model_copy(update={"cma_a2a_push_allowed_url": SINK_URL}),
        repository=runtime,
        provider=provider,
        pending_input=pending,
        scheduler_trigger=scheduler_trigger,
        push_trigger=push_trigger,
        push_http=httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(204))),
    )
    service = composition.service
    scheduler = composition.scheduler
    controller = create_controller(
        service,
        CONTROLLER_URL,
        push_trigger=push_trigger,
        push_allowed_url=SINK_URL,
        push_dispatcher=composition.push_dispatcher,
        push_config=composition.push_config,
    )
    return config, runtime, provider, service, scheduler, push_trigger, controller


def message(text: str, *, message_id: str | None = None) -> a2a.Message:
    return a2a.Message(
        message_id=message_id or str(uuid.uuid4()), role=a2a.Role.ROLE_USER, parts=[a2a.Part(text=text)]
    )


@pytest.mark.asyncio
async def test_official_push_crud_inline_and_current_delivery(components) -> None:
    _, runtime, _, service, scheduler, push_trigger, controller = components
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=controller), base_url=CONTROLLER_URL
    ) as http:
        client = await ClientFactory(
            ClientConfig(httpx_client=http, supported_protocol_bindings=["HTTP+JSON"], streaming=False)
        ).create_from_url(CONTROLLER_URL)
        card = await http.get("/.well-known/agent-card.json")
        assert card.json()["capabilities"]["pushNotifications"] is True
        assert any(
            "async-copilot-controller/v1" in item["uri"] for item in card.json()["capabilities"]["extensions"]
        )
        task = [
            item
            async for item in client.send_message(
                a2a.SendMessageRequest(
                    message=message("push CRUD"),
                    configuration=a2a.SendMessageConfiguration(return_immediately=True),
                )
            )
        ][0].task
        config = a2a.TaskPushNotificationConfig(task_id=task.id, id="a", url=SINK_URL)
        created = await client.create_task_push_notification_config(config)
        again = await client.create_task_push_notification_config(config)
        assert created.id == again.id == "a"
        with pytest.raises(InvalidParamsError):
            await client.create_task_push_notification_config(
                a2a.TaskPushNotificationConfig(task_id=task.id, id="a", url="http://evil.test/steal")
            )
        with pytest.raises(InvalidParamsError):
            await client.create_task_push_notification_config(
                a2a.TaskPushNotificationConfig(task_id=task.id, id="secret", url=SINK_URL, token="no")
            )
        with pytest.raises(InvalidParamsError):
            await client.create_task_push_notification_config(
                a2a.TaskPushNotificationConfig(
                    task_id=task.id,
                    id="auth",
                    url=SINK_URL,
                    authentication=a2a.AuthenticationInfo(scheme="Bearer", credentials="no"),
                )
            )
        with pytest.raises(TaskNotFoundError):
            await client.create_task_push_notification_config(
                a2a.TaskPushNotificationConfig(task_id=str(uuid.uuid4()), id="unknown", url=SINK_URL)
            )
        assert (
            await client.get_task_push_notification_config(
                a2a.GetTaskPushNotificationConfigRequest(task_id=task.id, id="a")
            )
        ).url == SINK_URL
        await client.create_task_push_notification_config(
            a2a.TaskPushNotificationConfig(task_id=task.id, id="b", url=SINK_URL)
        )
        listed = await client.list_task_push_notification_configs(
            a2a.ListTaskPushNotificationConfigsRequest(task_id=task.id, page_size=1)
        )
        assert [item.id for item in listed.configs] == ["a"]
        next_page = await client.list_task_push_notification_configs(
            a2a.ListTaskPushNotificationConfigsRequest(
                task_id=task.id, page_size=1, page_token=listed.next_page_token
            )
        )
        assert [item.id for item in next_page.configs] == ["b"]
        await client.delete_task_push_notification_config(
            a2a.DeleteTaskPushNotificationConfigRequest(task_id=task.id, id="a")
        )
        await client.delete_task_push_notification_config(
            a2a.DeleteTaskPushNotificationConfigRequest(task_id=task.id, id="a")
        )
        assert [
            item.id
            for item in (
                await client.list_task_push_notification_configs(
                    a2a.ListTaskPushNotificationConfigsRequest(task_id=task.id)
                )
            ).configs
        ] == ["b"]
        inline = a2a.TaskPushNotificationConfig(url=SINK_URL)
        inline_task = [
            item
            async for item in client.send_message(
                a2a.SendMessageRequest(
                    message=message("inline"),
                    configuration=a2a.SendMessageConfiguration(
                        return_immediately=True, task_push_notification_config=inline
                    ),
                )
            )
        ][0].task
        assert (
            len(
                (
                    await client.list_task_push_notification_configs(
                        a2a.ListTaskPushNotificationConfigsRequest(task_id=inline_task.id)
                    )
                ).configs
            )
            == 1
        )
        failed_id = str(uuid.uuid4())
        invalid = a2a.SendMessageRequest(
            message=message("accepted before config failure", message_id=failed_id),
            configuration=a2a.SendMessageConfiguration(
                return_immediately=True,
                task_push_notification_config=a2a.TaskPushNotificationConfig(url="http://evil.test/steal"),
            ),
        )
        with pytest.raises(InvalidParamsError):
            _ = [item async for item in client.send_message(invalid)]
        admitted_context = runtime.find_context_by_creation_message_id(failed_id)
        assert admitted_context is not None
        admitted_task = runtime.find_task_by_message_id(str(admitted_context["context_id"]), failed_id)
        assert admitted_task is not None
        invalid.configuration.task_push_notification_config.url = SINK_URL
        recovered = [item async for item in client.send_message(invalid)][0].task
        assert recovered.id == admitted_task["task_id"]
        assert push_trigger.pending
        await scheduler.run_once(task.context_id)
        assert (await service.get_task(task.id)).status.state == a2a.TaskState.TASK_STATE_COMPLETED
        assert runtime.get_task(task.id) is not None


@pytest.mark.asyncio
async def test_thread_to_controller_to_sink_tracer_bullet(components) -> None:
    config, runtime, provider, service, scheduler, trigger, controller = components
    Database(config).ensure_principal(config.dev_principal_id)
    threads = ThreadRepository(config)
    thread = threads.create_thread(config.dev_principal_id, "cma")
    thread_id = str(thread["thread_id"])
    threads.bind_surface(thread_id, "web", "local", f"web-{uuid.uuid4()}")
    threads.bind_surface(thread_id, "slack", "T123", f"slack-{uuid.uuid4()}")
    bus = LocalEventBus()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=controller), base_url=CONTROLLER_URL
    ) as controller_http:
        client = A2AControllerClient(
            AgentRegistry([AgentRegistration("cma", CONTROLLER_URL)]), controller_http
        )
        sink = create_sink(EventSinkRepository(threads), client, bus)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=sink), base_url="http://a2a-event-sink.test"
        ) as sink_http:
            dispatcher = PushDispatcher(PushRepository(runtime), service, trigger, sink_http)
            control = ThreadAgentService(threads, client, "http://a2a-event-sink.test/internal/a2a/events")
            first = await control.send_turn(thread_id, str(uuid.uuid4()), "hello")
            assert first.status.state == a2a.TaskState.TASK_STATE_SUBMITTED
            assert threads.get_thread(thread_id)["context_id"] == first.context_id
            assert threads.get_task_binding("cma", first.id) is not None
            await scheduler.run_once(first.context_id)
            await dispatcher.run_context(first.context_id)
            assert threads.get_task_binding("cma", first.id)["last_seen_task_state"] == "COMPLETED"
            events = bus.snapshot()["pending"]
            assert any(
                event["detail-type"] == "A2ATaskUpdated" and event["detail"]["taskState"] == "COMPLETED"
                for event in events
            )
            assert len(threads.list_surface_bindings(thread_id)) == 2
            assert provider.create_calls == 1
            await dispatcher.run_context(first.context_id)
            assert len(bus.snapshot()["pending"]) == len(events)
            stale = a2a.StreamResponse(
                task=a2a.Task(
                    id=first.id,
                    context_id=first.context_id,
                    status=a2a.TaskStatus(state=a2a.TaskState.TASK_STATE_WORKING),
                )
            )
            stale_id = hashlib.sha256(f"stale-{first.id}".encode()).hexdigest()
            headers = {"Content-Type": "application/a2a+json", "X-A2A-Delivery-Id": stale_id}
            stale_response = await sink_http.post(SINK_URL, content=MessageToJson(stale), headers=headers)
            assert stale_response.status_code == 200
            assert threads.get_task_binding("cma", first.id)["last_seen_task_state"] == "COMPLETED"
            count_after_stale = len(bus.snapshot()["pending"])
            duplicate_response = await sink_http.post(SINK_URL, content=MessageToJson(stale), headers=headers)
            assert duplicate_response.status_code == 200
            assert len(bus.snapshot()["pending"]) == count_after_stale

            class FailOnceBus:
                def __init__(self) -> None:
                    self.remaining = 1

                def publish(self, source: str, detail_type: str, detail: object) -> None:
                    if self.remaining:
                        self.remaining -= 1
                        raise RuntimeError("injected EventBridge failure")
                    bus.publish(source, detail_type, detail)

            failing_sink = create_sink(EventSinkRepository(threads), client, FailOnceBus())
            failure_id = hashlib.sha256(f"event-failure-{first.id}".encode()).hexdigest()
            failure_headers = {"Content-Type": "application/a2a+json", "X-A2A-Delivery-Id": failure_id}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=failing_sink), base_url="http://a2a-event-sink.test"
            ) as failing_http:
                assert (
                    await failing_http.post(SINK_URL, content=MessageToJson(stale), headers=failure_headers)
                ).status_code == 503
                assert (
                    await failing_http.post(SINK_URL, content=MessageToJson(stale), headers=failure_headers)
                ).status_code == 200
            assert (
                await sink_http.post(
                    SINK_URL,
                    content="not JSON",
                    headers={"Content-Type": "application/a2a+json", "X-A2A-Delivery-Id": "f" * 64},
                )
            ).status_code == 400
            unknown = a2a.StreamResponse(task=a2a.Task(id=str(uuid.uuid4()), context_id=first.context_id))
            assert (
                await sink_http.post(
                    SINK_URL,
                    content=MessageToJson(unknown),
                    headers={"Content-Type": "application/a2a+json", "X-A2A-Delivery-Id": "e" * 64},
                )
            ).status_code == 503

            class LosePublishedMark(EventSinkRepository):
                def __init__(self) -> None:
                    super().__init__(threads)
                    self.once = True

                def mark_published(self, agent_id: str, delivery_id: str) -> None:
                    if self.once:
                        self.once = False
                        raise TimeoutError("receipt update response lost")
                    super().mark_published(agent_id, delivery_id)

            lost_mark_sink = create_sink(LosePublishedMark(), client, bus)
            lost_id = hashlib.sha256(f"lost-publish-{first.id}".encode()).hexdigest()
            lost_headers = {"Content-Type": "application/a2a+json", "X-A2A-Delivery-Id": lost_id}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=lost_mark_sink),
                base_url="http://a2a-event-sink.test",
            ) as lost_http:
                before = len(bus.snapshot()["pending"])
                assert (
                    await lost_http.post(SINK_URL, content=MessageToJson(stale), headers=lost_headers)
                ).status_code == 503
                assert (
                    await lost_http.post(SINK_URL, content=MessageToJson(stale), headers=lost_headers)
                ).status_code == 200
                assert len(bus.snapshot()["pending"]) == before + 2
            with connect(config) as conn:
                receipt = conn.execute(
                    "SELECT * FROM a2a_task_event_receipts WHERE task_id = %s", (first.id,)
                ).fetchone()
                assert receipt is not None and receipt["published_at"] is not None
                assert "hello" not in str(receipt)
                count = conn.execute(
                    "SELECT COUNT(*) AS total FROM cma_contexts WHERE context_id = %s",
                    (first.context_id,),
                ).fetchone()
                assert count is not None and count["total"] == 1


@pytest.mark.asyncio
async def test_push_retry_and_maintenance_after_success(components) -> None:
    config, runtime, provider, service, scheduler, trigger, controller = components
    Database(config).ensure_principal(config.dev_principal_id)
    threads = ThreadRepository(config)
    thread_id = str(threads.create_thread(config.dev_principal_id, "cma")["thread_id"])
    bus = LocalEventBus()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=controller), base_url=CONTROLLER_URL
    ) as controller_http:
        client = A2AControllerClient(
            AgentRegistry([AgentRegistration("cma", CONTROLLER_URL)]), controller_http
        )
        control = ThreadAgentService(threads, client, "http://a2a-event-sink.test/internal/a2a/events")
        sink = create_sink(EventSinkRepository(threads), client, bus)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=sink), base_url="http://a2a-event-sink.test"
        ) as good_http:
            dispatcher = PushDispatcher(PushRepository(runtime), service, trigger, good_http)
            first = await control.send_turn(thread_id, str(uuid.uuid4()), "first")
            await scheduler.run_once(first.context_id)
            await dispatcher.run_context(first.context_id)
            assert PushRepository(runtime).get(first.id, "app-control-plane")["last_delivered_at"]
            prior = len(trigger.pending)
            assert await dispatcher.maintenance() >= 1
            assert len(trigger.pending) > prior

            provider.automatic = False
            second = await control.send_turn(thread_id, str(uuid.uuid4()), "second")
            await scheduler.run_once(second.context_id)

            async def unavailable(_request: httpx.Request) -> httpx.Response:
                return httpx.Response(503)

            async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as bad_http:
                failing = PushDispatcher(PushRepository(runtime), service, trigger, bad_http)
                await failing.run_context(second.context_id)
            failed = PushRepository(runtime).get(second.id, "app-control-plane")
            assert failed["attempt_count"] == 1 and failed["next_attempt_at"] is not None
            session_id = str(runtime.get_context(second.context_id)["cma_session_id"])
            provider.emit(session_id, {"type": "agent.message", "content": [{"type": "text", "text": "new"}]})
            with connect(config) as conn:
                conn.execute(
                    "UPDATE cma_push_configs SET next_attempt_at = CURRENT_TIMESTAMP - INTERVAL '1 second' "
                    "WHERE task_id = %s",
                    (second.id,),
                )
            await dispatcher.run_context(second.context_id)
            assert threads.get_task_binding("cma", second.id)["last_seen_task_state"] == "WORKING"
            first_fingerprint = PushRepository(runtime).get(second.id, "app-control-plane")[
                "last_delivered_fingerprint"
            ]
            provider.emit(
                session_id, {"type": "agent.message", "content": [{"type": "text", "text": "newer"}]}
            )
            await dispatcher.run_context(second.context_id)
            assert (
                PushRepository(runtime).get(second.id, "app-control-plane")["last_delivered_fingerprint"]
                != first_fingerprint
            )
            provider.finish(session_id, "latest")
            await scheduler.run_once(second.context_id)
            await dispatcher.run_context(second.context_id)
            assert threads.get_task_binding("cma", second.id)["last_seen_task_state"] == "COMPLETED"
            assert PushRepository(runtime).get(second.id, "app-control-plane")["attempt_count"] == 0

            third = await control.send_turn(thread_id, str(uuid.uuid4()), "third")

            async def missing(_request: httpx.Request) -> httpx.Response:
                return httpx.Response(404)

            async with httpx.AsyncClient(transport=httpx.MockTransport(missing)) as missing_http:
                rejecting = PushDispatcher(PushRepository(runtime), service, trigger, missing_http)
                await rejecting.run_context(third.context_id)
            rejected = PushRepository(runtime).get(third.id, "app-control-plane")
            assert rejected["permanent_failure_at"] is not None
            admin = ControllerAdmin(runtime, LocalSchedulerTrigger(), trigger)
            assert admin.list_failed_push()[0]["taskId"] == third.id
            assert admin.inspect_push(third.id, "app-control-plane")["permanentFailureAt"]
            with connect(config) as conn:
                conn.execute(
                    "UPDATE cma_push_configs SET delivery_claim_id = %s, "
                    "delivery_lease_expires_at = CURRENT_TIMESTAMP + INTERVAL '30 seconds' "
                    "WHERE task_id = %s AND config_id = %s",
                    (str(uuid.uuid4()), third.id, "app-control-plane"),
                )
            with pytest.raises(RuntimeError, match="active delivery claim"):
                admin.retry_push(third.id, "app-control-plane")
            with connect(config) as conn:
                conn.execute(
                    "UPDATE cma_push_configs SET delivery_lease_expires_at = "
                    "CURRENT_TIMESTAMP - INTERVAL '1 second' "
                    "WHERE task_id = %s AND config_id = %s",
                    (third.id, "app-control-plane"),
                )
            admin.retry_push(third.id, "app-control-plane")
            assert PushRepository(runtime).get(third.id, "app-control-plane")["permanent_failure_at"] is None
            await dispatcher.run_context(third.context_id)
            assert PushRepository(runtime).get(third.id, "app-control-plane")["last_delivered_at"]


@pytest.mark.asyncio
async def test_queued_cancel_schedules_push_wakeup(components) -> None:
    _, _, _, service, _, trigger, _ = components
    task = await service.send(message("queued cancel"))
    before = len(trigger.pending)
    cancelled = await service.cancel(task.id)
    assert cancelled.status.state == a2a.TaskState.TASK_STATE_CANCELED
    assert trigger.pending[before:] == [(task.context_id, "queued-cancelled")]


@pytest.mark.asyncio
async def test_normal_composition_delivers_push_without_manual_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = load_config().model_copy(update={"cma_a2a_push_allowed_url": SINK_URL})
    validate_local_database(config)
    # Earlier tests use different fake providers; maintenance must not replay their fixtures.
    with connect(config) as conn:
        conn.execute("DELETE FROM cma_push_configs")
    runtime = ControllerRuntimeRepository(config)
    provider = FakeCmaProvider()
    pending = FilePendingInputStore(tmp_path)
    sink_http: httpx.AsyncClient | None = None

    async def forward(request: httpx.Request) -> httpx.Response:
        assert sink_http is not None
        result = await sink_http.post(
            str(request.url), content=await request.aread(), headers=request.headers
        )
        return httpx.Response(result.status_code, content=result.content)

    async with httpx.AsyncClient(transport=httpx.MockTransport(forward)) as push_http:
        monkeypatch.setattr(controller_app, "load_config", lambda: config)
        monkeypatch.setattr(
            controller_app,
            "build_controller",
            lambda _config: build_controller(
                config, repository=runtime, provider=provider, pending_input=pending, push_http=push_http
            ),
        )
        monkeypatch.setenv("CMA_A2A_BASE_URL", CONTROLLER_URL)
        monkeypatch.setattr(controller_app, "_MAINTENANCE_INTERVAL_SECONDS", 0.2)
        controller = controller_app.production_app()
        composition = controller.state.controller_composition
        assert composition.service.push_trigger is composition.scheduler.push_trigger
        assert composition.service.push_trigger is composition.push_dispatcher.trigger
        Database(config).ensure_principal(config.dev_principal_id)
        threads = ThreadRepository(config)
        thread_id = str(threads.create_thread(config.dev_principal_id, "cma")["thread_id"])
        threads.bind_surface(thread_id, "web", "local", f"web-{uuid.uuid4()}")
        threads.bind_surface(thread_id, "slack", "T123", f"slack-{uuid.uuid4()}")
        bus = LocalEventBus()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=controller), base_url=CONTROLLER_URL
        ) as controller_http:
            client = A2AControllerClient(
                AgentRegistry([AgentRegistration("cma", CONTROLLER_URL)]), controller_http
            )
            sink = create_sink(EventSinkRepository(threads), client, bus)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=sink), base_url="http://a2a-event-sink.test"
            ) as receiver:
                sink_http = receiver
                async with controller.router.lifespan_context(controller):
                    card = await controller_http.get("/.well-known/agent-card.json")
                    assert card.json()["capabilities"]["pushNotifications"] is True
                    control = ThreadAgentService(
                        threads, client, "http://a2a-event-sink.test/internal/a2a/events"
                    )
                    task = await control.send_turn(thread_id, str(uuid.uuid4()), "normal composition")
                    for _ in range(150):
                        binding = threads.get_task_binding("cma", task.id)
                        if (
                            binding
                            and binding["last_seen_task_state"] == "COMPLETED"
                            and any(
                                event["detail-type"] == "A2ATaskUpdated"
                                for event in bus.snapshot()["pending"]
                            )
                        ):
                            break
                        await asyncio.sleep(0.02)
                    else:
                        raise AssertionError("Normal controller startup did not deliver push")
                    assert any(
                        event["detail-type"] == "A2ATaskUpdated" for event in bus.snapshot()["pending"]
                    )
                    assert len(threads.list_surface_bindings(thread_id)) == 2
                    assert provider.create_calls == 1
                    push_trigger = composition.push_trigger
                    assert push_trigger is not None
                    original_schedule = push_trigger.schedule_context

                    def drop_ordinary_wakeup(context_id: str, reason: str) -> None:
                        if reason == "push-maintenance":
                            original_schedule(context_id, reason)

                    monkeypatch.setattr(push_trigger, "schedule_context", drop_ordinary_wakeup)
                    later = await control.send_turn(thread_id, str(uuid.uuid4()), "lost push wakeup")
                    for _ in range(150):
                        binding = threads.get_task_binding("cma", later.id)
                        if (
                            binding
                            and binding["last_seen_task_state"] == "WORKING"
                            and any(
                                event["detail"]["taskId"] == later.id for event in bus.snapshot()["pending"]
                            )
                        ):
                            break
                        await asyncio.sleep(0.02)
                    else:
                        raise AssertionError("Periodic maintenance did not recover a lost push wakeup")


@pytest.mark.asyncio
async def test_lost_controller_and_push_setup_responses_recover_one_task(components) -> None:
    config, runtime, _, _service, _, _, controller = components
    Database(config).ensure_principal(config.dev_principal_id)
    threads = ThreadRepository(config)
    thread_id = str(threads.create_thread(config.dev_principal_id, "cma")["thread_id"])
    message_id = str(uuid.uuid4())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=controller), base_url=CONTROLLER_URL
    ) as http:
        real = A2AControllerClient(AgentRegistry([AgentRegistration("cma", CONTROLLER_URL)]), http)

        class FaultyClient:
            def __init__(self) -> None:
                self.lose_send = True
                self.lose_push = True

            def __getattr__(self, name: str):
                return getattr(real, name)

            async def send_message(self, *args, **kwargs):
                task = await real.send_message(*args, **kwargs)
                if self.lose_send:
                    self.lose_send = False
                    raise TimeoutError("controller response lost")
                return task

            async def create_push_config(self, *args, **kwargs):
                config = await real.create_push_config(*args, **kwargs)
                if self.lose_push:
                    self.lose_push = False
                    raise TimeoutError("push config response lost")
                return config

        faulty = FaultyClient()
        control = ThreadAgentService(threads, faulty, "http://a2a-event-sink.test/internal/a2a/events")
        with pytest.raises(TimeoutError, match="controller response lost"):
            await control.send_turn(thread_id, message_id, "one task")
        context = runtime.find_context_by_creation_message_id(message_id)
        assert context is not None
        original = runtime.find_task_by_message_id(str(context["context_id"]), message_id)
        assert original is not None
        with connect(config) as conn:
            conn.execute(
                "UPDATE agent_threads SET context_init_lease_expires_at = "
                "CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE thread_id = %s",
                (thread_id,),
            )
        from managed_agents_app.agent_control_plane.service import RetryablePushSetupError

        with pytest.raises(RetryablePushSetupError):
            await control.send_turn(thread_id, message_id, "one task")
        recovered = await control.send_turn(thread_id, message_id, "one task")
        assert recovered.id == original["task_id"]
        assert threads.get_thread(thread_id)["context_id"] == context["context_id"]
        assert len((await real.list_push_configs("cma", recovered.id)).configs) == 1
        with connect(config) as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS total FROM cma_tasks WHERE context_id = %s", (recovered.context_id,)
            ).fetchone()
            assert count is not None and count["total"] == 1


@pytest.mark.asyncio
async def test_definitive_first_turn_rejection_releases_context_claim(components) -> None:
    config, _runtime, _, _, _, _, controller = components
    Database(config).ensure_principal(config.dev_principal_id)
    threads = ThreadRepository(config)
    thread_id = str(threads.create_thread(config.dev_principal_id, "cma")["thread_id"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=controller), base_url=CONTROLLER_URL
    ) as http:
        real = A2AControllerClient(AgentRegistry([AgentRegistration("cma", CONTROLLER_URL)]), http)

        class RejectingClient:
            def __getattr__(self, name: str):
                return getattr(real, name)

            async def send_message(self, *_args, **_kwargs):
                raise DefinitiveA2ARequestError("controller rejected request")

        service = ThreadAgentService(
            threads, RejectingClient(), "http://a2a-event-sink.test/internal/a2a/events"
        )
        with pytest.raises(DefinitiveA2ARequestError):
            await service.send_turn(thread_id, str(uuid.uuid4()), "reject")
        thread = threads.get_thread(thread_id)
        assert thread["context_state"] == "uninitialized"
        assert thread["context_init_claim_id"] is None


@pytest.mark.asyncio
async def test_concurrent_first_turns_share_one_context(components) -> None:
    config, runtime, _, _, _, _, controller = components
    Database(config).ensure_principal(config.dev_principal_id)
    threads = ThreadRepository(config)
    thread_id = str(threads.create_thread(config.dev_principal_id, "cma")["thread_id"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=controller), base_url=CONTROLLER_URL
    ) as http:
        client = A2AControllerClient(AgentRegistry([AgentRegistration("cma", CONTROLLER_URL)]), http)
        control = ThreadAgentService(threads, client, "http://a2a-event-sink.test/internal/a2a/events")
        first, second = await asyncio.gather(
            control.send_turn(thread_id, str(uuid.uuid4()), "first"),
            control.send_turn(thread_id, str(uuid.uuid4()), "second"),
        )
        assert first.context_id == second.context_id
        assert first.id != second.id
        with connect(config) as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS total FROM cma_contexts WHERE context_id = %s",
                (first.context_id,),
            ).fetchone()
            assert count is not None and count["total"] == 1
        assert len(runtime.list_tasks(first.context_id)) == 2
