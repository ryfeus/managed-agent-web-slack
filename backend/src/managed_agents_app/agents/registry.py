"""Private A2A endpoints selected by application agent ID."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from managed_agents_app.config import AppConfig
from managed_agents_app.protocols.controller_profile import ASYNC_COPILOT_PROFILE_URI

DEFAULT_APPLICATION_AGENT_ID = "cma"


@dataclass(frozen=True)
class AgentRegistration:
    agent_id: str
    endpoint: str
    transport: Literal["http-json"] = "http-json"
    required_profile: str = ASYNC_COPILOT_PROFILE_URI

    def __post_init__(self) -> None:
        parsed = urlsplit(self.endpoint)
        if not self.agent_id or parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Agent registration requires an ID and an HTTP(S) endpoint")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Agent endpoint must not contain credentials or a fragment")


class AgentRegistry:
    def __init__(self, registrations: Iterable[AgentRegistration], default_agent_id: str | None = None):
        self._items: dict[str, AgentRegistration] = {}
        for registration in registrations:
            if registration.agent_id in self._items:
                raise ValueError(f"Duplicate agent ID: {registration.agent_id}")
            self._items[registration.agent_id] = registration
        if default_agent_id is not None and default_agent_id not in self._items:
            raise ValueError("Default agent is not registered")
        self.default_agent_id = default_agent_id

    def get(self, agent_id: str) -> AgentRegistration | None:
        return self._items.get(agent_id)

    def default(self) -> AgentRegistration | None:
        return self.get(self.default_agent_id) if self.default_agent_id else None


def from_config(config: AppConfig) -> AgentRegistry:
    endpoint = config.cma_a2a_endpoint.strip()
    if not endpoint:
        return AgentRegistry(())
    return AgentRegistry(
        (AgentRegistration(agent_id=DEFAULT_APPLICATION_AGENT_ID, endpoint=endpoint),),
        default_agent_id=DEFAULT_APPLICATION_AGENT_ID,
    )
