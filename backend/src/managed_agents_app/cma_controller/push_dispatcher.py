"""Leased, level-triggered A2A push delivery."""

from __future__ import annotations

import asyncio
import hashlib
import logging

import httpx
from a2a.types import a2a_pb2 as a2a
from google.protobuf.json_format import MessageToJson  # type: ignore[import-untyped]

from managed_agents_app.cma_controller.push_repository import PushRepository
from managed_agents_app.cma_controller.push_trigger import PushTrigger
from managed_agents_app.cma_controller.service import ControllerService
from managed_agents_app.cma_controller.task_view import TaskViewOptions
from managed_agents_app.protocols.controller_profile import A2A_DELIVERY_ID_HEADER

logger = logging.getLogger(__name__)
_BACKOFF = (5, 15, 30, 60, 120, 300)


class PushDispatcher:
    def __init__(
        self,
        repository: PushRepository,
        service: ControllerService,
        trigger: PushTrigger,
        http: httpx.AsyncClient,
    ) -> None:
        self.repository = repository
        self.service = service
        self.trigger = trigger
        self.http = http

    async def run_context(self, context_id: str) -> None:
        rows = await asyncio.to_thread(self.repository.list_context, context_id)
        for row in rows:
            await self._deliver(str(row["task_id"]), str(row["config_id"]))

    async def _deliver(self, task_id: str, config_id: str) -> None:
        claim = await asyncio.to_thread(self.repository.claim, task_id, config_id)
        if claim is None:
            return
        try:
            row = await asyncio.to_thread(self.repository.get, task_id, config_id)
            if row is None or not await asyncio.to_thread(self.repository.claimed, task_id, config_id, claim):
                return
            task = await self.service.get_task(task_id, options=TaskViewOptions(history_length=1))
            envelope = a2a.StreamResponse(task=task)
            fingerprint = hashlib.sha256(envelope.SerializeToString(deterministic=True)).hexdigest()
            if row["last_delivered_fingerprint"] == fingerprint:
                return
            delivery_id = hashlib.sha256(f"{config_id}\x00{task_id}\x00{fingerprint}".encode()).hexdigest()
            if not await asyncio.to_thread(self.repository.claimed, task_id, config_id, claim):
                return
            try:
                response = await self.http.post(
                    str(row["url"]),
                    content=MessageToJson(envelope).encode(),
                    headers={"Content-Type": "application/a2a+json", A2A_DELIVERY_ID_HEADER: delivery_id},
                    timeout=10,
                    follow_redirects=False,
                )
                if 200 <= response.status_code < 300:
                    await asyncio.to_thread(
                        self.repository.finish, task_id, config_id, claim, fingerprint=fingerprint
                    )
                    return
                transient = response.status_code in {408, 429} or response.status_code >= 500
            except httpx.HTTPError:
                transient = True
            attempts = int(row["attempt_count"] or 0)
            delay = _BACKOFF[min(attempts, len(_BACKOFF) - 1)] if transient else None
            await asyncio.to_thread(
                self.repository.finish,
                task_id,
                config_id,
                claim,
                fingerprint=None,
                retry_seconds=delay,
                permanent=not transient,
            )
            if transient:
                logger.warning("a2a_push_retry", extra={"task_id": task_id, "config_id": config_id})
            else:
                logger.error("a2a_push_permanent_failure", extra={"task_id": task_id, "config_id": config_id})
        except Exception:
            logger.exception("a2a_push_dispatch_failed", extra={"task_id": task_id, "config_id": config_id})
            await asyncio.to_thread(
                self.repository.finish, task_id, config_id, claim, fingerprint=None, retry_seconds=5
            )
        finally:
            await asyncio.to_thread(self.repository.release, task_id, config_id, claim)

    async def maintenance(self) -> int:
        total = 0
        after = ""
        while True:
            contexts = await asyncio.to_thread(self.repository.contexts_for_maintenance, after)
            for context_id in contexts:
                self.trigger.schedule_context(context_id, "push-maintenance")
            total += len(contexts)
            if len(contexts) < 500:
                return total
            after = contexts[-1]
