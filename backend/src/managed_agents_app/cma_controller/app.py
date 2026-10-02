"""Standalone local/private A2A ASGI application; no public deployment route."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from a2a.server.routes import create_agent_card_routes, create_rest_routes
from a2a.types import a2a_pb2 as a2a
from a2a.utils.constants import PROTOCOL_VERSION_CURRENT, TransportProtocol
from fastapi import FastAPI

from managed_agents_app.cma_controller.composition import ControllerComposition, build_controller
from managed_agents_app.cma_controller.push_config import PushConfigService
from managed_agents_app.cma_controller.push_dispatcher import PushDispatcher
from managed_agents_app.cma_controller.push_trigger import AsyncLocalPushTrigger, PushTrigger, SqsPushTrigger
from managed_agents_app.cma_controller.request_handler import CmaA2ARequestHandler
from managed_agents_app.cma_controller.scheduler import CmaScheduler
from managed_agents_app.cma_controller.service import ControllerService
from managed_agents_app.cma_controller.trigger import AsyncLocalSchedulerTrigger
from managed_agents_app.config import load_config
from managed_agents_app.protocols.controller_profile import (
    ASYNC_COPILOT_PROFILE_URI,
    HUMAN_INPUT_EXTENSION_URI,
)

logger = logging.getLogger(__name__)
_MAINTENANCE_INTERVAL_SECONDS = 60


async def _maintain_push(dispatcher: PushDispatcher) -> None:
    while True:
        try:
            await dispatcher.maintenance()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("local_a2a_push_maintenance_failed")
        await asyncio.sleep(_MAINTENANCE_INTERVAL_SECONDS)


def agent_card(base_url: str, push_enabled: bool = False) -> a2a.AgentCard:
    return a2a.AgentCard(
        name="CMA A2A Controller",
        description="Durable A2A controller for Claude Managed Agents",
        version="0.2.0",
        supported_interfaces=[
            a2a.AgentInterface(
                url=base_url.rstrip("/"),
                protocol_binding=TransportProtocol.HTTP_JSON.value,
                protocol_version=PROTOCOL_VERSION_CURRENT,
            )
        ],
        capabilities=a2a.AgentCapabilities(
            streaming=True,
            push_notifications=push_enabled,
            extensions=[
                a2a.AgentExtension(uri=HUMAN_INPUT_EXTENSION_URI, required=False),
                *(
                    [a2a.AgentExtension(uri=ASYNC_COPILOT_PROFILE_URI, required=False)]
                    if push_enabled
                    else []
                ),
            ],
        ),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            a2a.AgentSkill(
                id="cma", name="Claude Managed Agent", description="Conversational agent", tags=["assistant"]
            )
        ],
    )


def create_app(
    service: ControllerService,
    base_url: str = "http://127.0.0.1:8081",
    *,
    local_scheduler: CmaScheduler | None = None,
    push_trigger: PushTrigger | None = None,
    push_dispatcher: PushDispatcher | None = None,
    push_config: PushConfigService | None = None,
    push_allowed_url: str | None = None,
    composition: ControllerComposition | None = None,
) -> FastAPI:
    allowed_url = (
        push_allowed_url
        if push_allowed_url is not None
        else service.repository.config.cma_a2a_push_allowed_url
    )
    if allowed_url and (push_trigger is None or push_dispatcher is None):
        raise RuntimeError("Push callback policy requires a runnable push worker")
    if push_trigger is not None and service.push_trigger is not push_trigger:
        raise RuntimeError("Service and push configuration must share a push trigger")
    if push_dispatcher is not None and push_dispatcher.trigger is not push_trigger:
        raise RuntimeError("Dispatcher and service must share a push trigger")
    if allowed_url and not isinstance(push_trigger, (AsyncLocalPushTrigger, SqsPushTrigger)):
        raise RuntimeError("Push trigger has no runnable worker")
    if allowed_url and push_config is None:
        raise RuntimeError("Push callback policy requires a push configuration service")
    card = agent_card(base_url, push_enabled=bool(allowed_url and push_dispatcher))
    handler = CmaA2ARequestHandler(service, card, push_config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        maintenance: asyncio.Task[None] | None = None
        if local_scheduler is not None:
            if not isinstance(service.trigger, AsyncLocalSchedulerTrigger):
                raise RuntimeError("Local scheduler requires an async local trigger")
            service.trigger.start(local_scheduler)
        if isinstance(push_trigger, AsyncLocalPushTrigger):
            assert push_dispatcher is not None
            push_trigger.start(push_dispatcher)
            maintenance = asyncio.create_task(_maintain_push(push_dispatcher))
        try:
            yield
        finally:
            if maintenance is not None:
                maintenance.cancel()
                await asyncio.gather(maintenance, return_exceptions=True)
            if isinstance(push_trigger, AsyncLocalPushTrigger):
                await push_trigger.stop()
            if isinstance(service.trigger, AsyncLocalSchedulerTrigger):
                await service.trigger.stop()
            if composition is not None:
                await composition.close()

    app = FastAPI(routes=[*create_agent_card_routes(card), *create_rest_routes(handler)], lifespan=lifespan)
    app.state.push_trigger = push_trigger
    app.state.controller_composition = composition
    return app


def production_app() -> FastAPI:
    config = load_config()
    composition = build_controller(config)
    return create_app(
        composition.service,
        os.getenv("CMA_A2A_BASE_URL", f"http://127.0.0.1:{os.getenv('PORT', '8081')}"),
        local_scheduler=(
            composition.scheduler
            if isinstance(composition.scheduler_trigger, AsyncLocalSchedulerTrigger)
            else None
        ),
        push_trigger=composition.push_trigger,
        push_dispatcher=composition.push_dispatcher,
        push_config=composition.push_config,
        composition=composition,
    )
