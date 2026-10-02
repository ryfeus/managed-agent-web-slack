from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from managed_agents_app.a2a_client.client import A2AControllerClient
from managed_agents_app.a2a_event_sink.app import create_app as create_sink
from managed_agents_app.a2a_event_sink.repository import EventSinkRepository
from managed_agents_app.agents.registry import AgentRegistration, AgentRegistry
from managed_agents_app.cma_controller.app import create_app as create_controller
from managed_agents_app.cma_controller.composition import build_controller
from managed_agents_app.cma_controller.pending_input import FilePendingInputStore
from managed_agents_app.cma_controller.push_dispatcher import PushDispatcher
from managed_agents_app.cma_controller.push_repository import PushRepository
from managed_agents_app.cma_controller.push_trigger import AsyncLocalPushTrigger
from managed_agents_app.cma_controller.trigger import LocalSchedulerTrigger
from managed_agents_app.config import AppConfig
from managed_agents_app.db import Database
from managed_agents_app.db.thread_repository import ThreadRepository
from managed_agents_app.runtime import Runtime
from managed_agents_app.testing.fake_cma_provider import FakeCmaProvider
from managed_agents_app.testing.faults import FaultInjector
from managed_agents_app.testing.local_event_bus import LocalEventBus
from managed_agents_app.testing.local_queue import LocalQueue
from managed_agents_app.testing.recording_slack import RecordingSlack

CONTROLLER_URL = "http://a2a.local"
SINK_BASE = "http://a2a-sink.local/internal/a2a/events"


class LocalA2AHarness:
    def __init__(self, config: AppConfig, bus: LocalEventBus) -> None:
        self.bus = bus
        self.provider = FakeCmaProvider()
        self.scheduler_trigger = LocalSchedulerTrigger()
        self.push_trigger = AsyncLocalPushTrigger()
        self.pending_directory = Path(tempfile.mkdtemp(prefix="slack-a2a-e2e-"))
        self.config = config.model_copy(update={"cma_a2a_push_allowed_url": SINK_BASE + "/cma"})
        self.composition = build_controller(
            self.config,
            provider=self.provider,
            pending_input=FilePendingInputStore(self.pending_directory),
            scheduler_trigger=self.scheduler_trigger,
            push_trigger=self.push_trigger,
            push_http=httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(204))),
        )
        self.controller = create_controller(
            self.composition.service,
            CONTROLLER_URL,
            push_trigger=self.push_trigger,
            push_allowed_url=SINK_BASE + "/cma",
            push_dispatcher=self.composition.push_dispatcher,
            push_config=self.composition.push_config,
        )

    async def drain(self) -> None:
        # Exercise real controller scheduling and the generic push sink in order.
        delayed_reconciled: set[str] = set()
        for _ in range(30):
            scheduled = list(self.scheduler_trigger.pending)
            self.scheduler_trigger.pending.clear()
            for context_id, _, delay in scheduled:
                if delay:
                    if context_id in delayed_reconciled:
                        continue
                    delayed_reconciled.add(context_id)
                await self.composition.scheduler.run_once(context_id)
            pushed = list(self.push_trigger.pending)
            self.push_trigger.pending.clear()
            if pushed:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=self.controller), base_url=CONTROLLER_URL
                ) as controller_http:
                    client = A2AControllerClient(
                        AgentRegistry([AgentRegistration("cma", CONTROLLER_URL)]), controller_http
                    )
                    sink = create_sink(EventSinkRepository(ThreadRepository(self.config)), client, self.bus)
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=sink), base_url="http://a2a-sink.local"
                    ) as sink_http:
                        dispatcher = PushDispatcher(
                            PushRepository(self.composition.repository),
                            self.composition.service,
                            self.push_trigger,
                            sink_http,
                        )
                        for context_id, _ in pushed:
                            await dispatcher.run_context(context_id)
            if not scheduled and not pushed:
                return
        raise RuntimeError("Local A2A controller did not quiesce")

    def snapshot(self) -> dict[str, Any]:
        return {
            "sessions": [session.model_dump(mode="json") for session in self.provider.sessions.values()],
            "events": self.provider.events,
            "create_calls": self.provider.create_calls,
            "send_calls": self.provider.send_calls,
            "confirm_calls": self.provider.confirm_calls,
            "custom_result_calls": self.provider.custom_result_calls,
            "interrupt_calls": self.provider.interrupt_calls,
        }

    def reset(self) -> None:
        self.provider.sessions.clear()
        self.provider.events.clear()
        self.provider.listeners.clear()
        self.provider.scripted_turns.clear()
        self.provider.create_calls = self.provider.send_calls = 0
        self.provider.confirm_calls = self.provider.custom_result_calls = self.provider.interrupt_calls = 0
        self.provider.automatic = True
        self.scheduler_trigger.pending.clear()
        self.push_trigger.pending.clear()
        for key in self.composition.pending_input.list_keys():
            self.composition.pending_input.delete(key)


def validate_local_database(config: AppConfig) -> None:
    url = urlparse(config.database_url)
    if (
        url.scheme not in {"postgres", "postgresql"}
        or bool(url.query)
        or config.app_env != "e2e"
        or config.database_mode != "postgres"
        or url.hostname not in {"127.0.0.1", "localhost"}
        or url.path != "/managed_agents_e2e"
    ):
        raise ValueError("E2E requires a loopback PostgreSQL database named managed_agents_e2e")


def create_runtime(config: AppConfig) -> Runtime:
    validate_local_database(config)
    config = config.model_copy(
        update={
            "cma_a2a_endpoint": CONTROLLER_URL,
            "a2a_event_sink_url": SINK_BASE,
        }
    )
    bus = LocalEventBus(auto_drain=os.getenv("LOCAL_EVENT_BUS_AUTO_DRAIN") == "true")
    runtime = Runtime(config, Database(config), RecordingSlack(), bus, FaultInjector())
    harness = LocalA2AHarness(config, bus)
    runtime.a2a_transport = httpx.ASGITransport(app=harness.controller)
    runtime.local_a2a = harness
    from managed_agents_app.handlers import agent_input_sqs

    queue = LocalQueue(lambda event: agent_input_sqs.handle_sqs_event(runtime, event))
    bus.agent_input_queue = queue

    def dispatch(event: dict[str, Any]) -> None:
        from managed_agents_app.handlers import slack_projector

        if event["detail-type"] in {
            "SlackTaskProjectionRequested",
            "SlackWorkStarted",
            "A2ATaskUpdated",
            "SlackControlReplyRequested",
        }:
            slack_projector.handle_domain_event(runtime, event)
        elif event["detail-type"] in {
            "SlackMessageReceived",
            "SlackThreadStopRequested",
            "SlackToolConfirmationRequested",
            "SlackFeedbackReceived",
            "SlackShortcutReceived",
            "SlackThreadLinkRequested",
            "SlackThreadLinkShared",
        }:
            queue.send(event)
        else:
            raise ValueError(f"Unknown domain event: {event['detail-type']}")

    bus.dispatch = dispatch
    return runtime
