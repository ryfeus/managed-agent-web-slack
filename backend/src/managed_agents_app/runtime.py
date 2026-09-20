from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property, lru_cache

from managed_agents_app.config import AppConfig, load_config
from managed_agents_app.db import Database
from managed_agents_app.faults import Faults, NoFaults
from managed_agents_app.ports.agent import AgentGateway
from managed_agents_app.ports.events import EventBus
from managed_agents_app.ports.slack import SlackGateway


@dataclass
class Runtime:
    config: AppConfig
    db: Database
    agent: AgentGateway
    slack: SlackGateway
    events: EventBus
    faults: Faults = field(default_factory=NoFaults)


class ProductionRuntime:
    """Lazy clients keep health/auth/ingress independent of unused credentials."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.faults: Faults = NoFaults()

    @cached_property
    def db(self) -> Database:
        return Database(self.config)

    @cached_property
    def agent(self) -> AgentGateway:
        from managed_agents_app.managed_agent import ManagedAgentClient

        return ManagedAgentClient(self.config)

    @cached_property
    def slack(self) -> SlackGateway:
        from managed_agents_app.slack import SlackClient

        if not self.config.slack_bot_token:
            raise RuntimeError("Slack bot token is not configured")
        return SlackClient(self.config.slack_bot_token)

    @cached_property
    def events(self) -> EventBus:
        from managed_agents_app.events import EventBridgeEventBus

        return EventBridgeEventBus(self.config.event_bus_name)


@lru_cache(maxsize=8)
def _runtime(config: AppConfig) -> Runtime:
    if config.app_env == "e2e":
        from managed_agents_app.testing.factory import create_runtime

        return create_runtime(config)
    # Runtime's structural attributes are shared with the lazy production composition.
    from typing import cast

    return cast(Runtime, ProductionRuntime(config))


def get_runtime(config: AppConfig | None = None) -> Runtime:
    return _runtime(config or load_config())
