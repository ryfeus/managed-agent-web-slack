"""One controller wiring path for ASGI, queue workers, and administration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import httpx

from managed_agents_app.cma_controller.pending_input import FilePendingInputStore, PendingInputStore
from managed_agents_app.cma_controller.pending_input_s3 import S3PendingInputStore
from managed_agents_app.cma_controller.provider import AnthropicCmaProvider, CmaProvider
from managed_agents_app.cma_controller.push_config import PushConfigService
from managed_agents_app.cma_controller.push_dispatcher import PushDispatcher
from managed_agents_app.cma_controller.push_repository import PushRepository
from managed_agents_app.cma_controller.push_trigger import AsyncLocalPushTrigger, PushTrigger
from managed_agents_app.cma_controller.runtime_repository import ControllerRuntimeRepository
from managed_agents_app.cma_controller.scheduler import CmaScheduler
from managed_agents_app.cma_controller.service import ControllerService
from managed_agents_app.cma_controller.trigger import AsyncLocalSchedulerTrigger, SchedulerTrigger
from managed_agents_app.cma_controller.trigger_sqs import production_push_trigger, production_trigger
from managed_agents_app.config import AppConfig
from managed_agents_app.managed_agent.client import ManagedAgentClient


@dataclass
class ControllerComposition:
    repository: ControllerRuntimeRepository
    provider: CmaProvider
    pending_input: PendingInputStore
    scheduler_trigger: SchedulerTrigger
    push_trigger: PushTrigger | None
    service: ControllerService
    scheduler: CmaScheduler
    push_config: PushConfigService | None
    push_dispatcher: PushDispatcher | None
    push_http: httpx.AsyncClient | None
    owns_push_http: bool

    async def close(self) -> None:
        if self.owns_push_http and self.push_http is not None:
            await self.push_http.aclose()


def build_controller(
    config: AppConfig,
    *,
    repository: ControllerRuntimeRepository | None = None,
    provider: CmaProvider | None = None,
    pending_input: PendingInputStore | None = None,
    scheduler_trigger: SchedulerTrigger | None = None,
    push_trigger: PushTrigger | None = None,
    push_http: httpx.AsyncClient | None = None,
    with_dispatcher: bool = True,
) -> ControllerComposition:
    repository = repository or ControllerRuntimeRepository(config)
    provider = provider or AnthropicCmaProvider(ManagedAgentClient(config))
    pending_input = pending_input or (
        S3PendingInputStore(config.cma_pending_input_bucket)
        if config.cma_pending_input_bucket
        else FilePendingInputStore(Path(os.getenv("CMA_PENDING_INPUT_DIR", "/tmp/cma-pending-input")))
    )
    scheduler_trigger = scheduler_trigger or (
        production_trigger(config.cma_scheduler_queue_url)
        if config.cma_scheduler_queue_url
        else AsyncLocalSchedulerTrigger()
    )
    if push_trigger is None:
        if config.cma_push_queue_url:
            push_trigger = production_push_trigger(config.cma_push_queue_url)
        elif config.cma_a2a_push_allowed_url and not config.cma_scheduler_queue_url:
            push_trigger = AsyncLocalPushTrigger()
    if config.cma_a2a_push_allowed_url and push_trigger is None:
        raise RuntimeError("Push callback policy requires a runnable push worker")
    if (
        config.cma_a2a_push_allowed_url
        and not isinstance(push_trigger, AsyncLocalPushTrigger)
        and not config.cma_push_queue_url
    ):
        raise RuntimeError("Push callback policy requires a runnable push worker")

    service = ControllerService(repository, provider, pending_input, scheduler_trigger, push_trigger)
    scheduler = CmaScheduler(repository, provider, pending_input, scheduler_trigger, push_trigger)
    push_repository = PushRepository(repository) if push_trigger is not None else None
    owned = with_dispatcher and push_trigger is not None and push_http is None
    if owned:
        push_http = httpx.AsyncClient(timeout=10)
    dispatcher = (
        PushDispatcher(push_repository, service, push_trigger, push_http)
        if with_dispatcher
        and push_repository is not None
        and push_http is not None
        and push_trigger is not None
        else None
    )
    push_config = (
        PushConfigService(push_repository, config.cma_a2a_push_allowed_url, push_trigger)
        if config.cma_a2a_push_allowed_url and push_repository is not None and push_trigger is not None
        else None
    )
    return ControllerComposition(
        repository,
        provider,
        pending_input,
        scheduler_trigger,
        push_trigger,
        service,
        scheduler,
        push_config,
        dispatcher,
        push_http,
        owned,
    )
