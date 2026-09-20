from __future__ import annotations

import os
from typing import Any, cast
from urllib.parse import urlparse

from managed_agents_app.config import AppConfig
from managed_agents_app.db import Database
from managed_agents_app.runtime import Runtime
from managed_agents_app.testing.fake_agent import FakeManagedAgent
from managed_agents_app.testing.faults import FaultInjector
from managed_agents_app.testing.local_event_bus import LocalEventBus
from managed_agents_app.testing.local_queue import LocalQueue
from managed_agents_app.testing.recording_slack import RecordingSlack


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
    bus = LocalEventBus(auto_drain=os.getenv("LOCAL_EVENT_BUS_AUTO_DRAIN") == "true")
    runtime = Runtime(config, Database(config), FakeManagedAgent(bus), RecordingSlack(), bus, FaultInjector())
    from managed_agents_app.handlers import agent_input_sqs

    queue = LocalQueue(lambda event: agent_input_sqs.handle_sqs_event(runtime, event))
    bus.agent_input_queue = queue

    def dispatch(event: dict[str, Any]) -> None:
        from managed_agents_app.handlers import slack_projector

        if event["detail-type"] in {
            "SlackProjectionRequested",
            "SlackWorkStarted",
            "ManagedAgentSessionChanged",
            "SlackControlReplyRequested",
        }:
            slack_projector.handle_domain_event(runtime, event)
        elif event["detail-type"] in {
            "SlackMessageReceived",
            "SlackSessionStopRequested",
            "SlackToolConfirmationRequested",
            "SlackFeedbackReceived",
            "SlackShortcutReceived",
            "SlackSessionLinkRequested",
            "SlackSessionLinkShared",
        }:
            queue.send(event)
        else:
            raise ValueError(f"Unknown domain event: {event['detail-type']}")

    bus.dispatch = dispatch
    bus.after_delivery = cast(FakeManagedAgent, runtime.agent).flush_notifications
    queue.after_delivery = cast(FakeManagedAgent, runtime.agent).flush_notifications
    return runtime
