"""Create an A2A client within the event loop handling one request."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from managed_agents_app.a2a_client.client import A2AControllerClient
from managed_agents_app.agent_control_plane.service import ThreadAgentService
from managed_agents_app.agents.registry import from_config
from managed_agents_app.runtime import Runtime


@asynccontextmanager
async def application_control_plane(runtime: Runtime) -> AsyncIterator[ThreadAgentService]:
    config = runtime.config
    if not config.a2a_event_sink_url:
        raise RuntimeError("A2A_EVENT_SINK_URL is required for application A2A execution")
    transport = getattr(runtime, "a2a_transport", None)
    async with httpx.AsyncClient(transport=transport, timeout=20) as http:
        client = A2AControllerClient(from_config(config), http)
        yield ThreadAgentService(runtime.threads, client, config.a2a_event_sink_url)
