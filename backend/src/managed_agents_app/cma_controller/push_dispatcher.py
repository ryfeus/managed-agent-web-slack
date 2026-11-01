"""Leased A2A callbacks with bounded, explicitly queued retries."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from datetime import UTC, datetime

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
        delays = []
        for row in rows:
            if (
                row["a2a_state"] in {"COMPLETED", "CANCELED", "FAILED", "REJECTED"}
                and row["last_delivered_fingerprint"]
            ):
                # Duplicate terminal wakeups must not compete for leases and create
                # more wakeups. Compare canonical content; state alone is insufficient.
                try:
                    task = await self.service.get_task(
                        str(row["task_id"]), options=TaskViewOptions(history_length=1)
                    )
                    fingerprint = hashlib.sha256(
                        a2a.StreamResponse(task=task).SerializeToString(deterministic=True)
                    ).hexdigest()
                    if row["last_delivered_fingerprint"] == fingerprint:
                        continue
                except Exception:
                    # Unreadable/changed snapshots use the normal bounded delivery path.
                    pass
            delay = await self._deliver(str(row["task_id"]), str(row["config_id"]))
            if delay is not None:
                delays.append(delay)
        if delays:
            # Enqueue errors must fail the SQS record. A redelivery before the due
            # time requeues the remaining delay instead of acknowledging lost work.
            await asyncio.to_thread(self.trigger.schedule_context, context_id, "push-retry", min(delays))

    async def _deliver(self, task_id: str, config_id: str) -> int | None:
        claim = await asyncio.to_thread(self.repository.claim, task_id, config_id)
        if claim is None:
            row = await asyncio.to_thread(self.repository.get, task_id, config_id)
            if row is None or row["permanent_failure_at"] is not None:
                return None
            due = [row[key] for key in ("next_attempt_at", "delivery_lease_expires_at") if row[key]]
            return min(900, max(1, math.ceil((max(due) - datetime.now(UTC)).total_seconds()))) if due else 1
        try:
            row = await asyncio.to_thread(self.repository.get, task_id, config_id)
            if row is None:
                return None
            try:
                task = await self.service.get_task(task_id, options=TaskViewOptions(history_length=1))
                envelope = a2a.StreamResponse(task=task)
                fingerprint = hashlib.sha256(envelope.SerializeToString(deterministic=True)).hexdigest()
                if row["last_delivered_fingerprint"] == fingerprint:
                    return None
                delivery_id = hashlib.sha256(
                    f"{config_id}\x00{task_id}\x00{fingerprint}".encode()
                ).hexdigest()
                # Recheck once after the provider read, including a concurrent delete.
                if not await asyncio.to_thread(self.repository.claimed, task_id, config_id, claim):
                    return 1
                response = await self.http.post(
                    str(row["url"]),
                    content=MessageToJson(envelope).encode(),
                    headers={"Content-Type": "application/a2a+json", A2A_DELIVERY_ID_HEADER: delivery_id},
                    timeout=10,
                    follow_redirects=False,
                )
            except Exception:
                logger.exception(
                    "a2a_push_dispatch_failed", extra={"task_id": task_id, "config_id": config_id}
                )
                transient = True
            else:
                if 200 <= response.status_code < 300:
                    finished = await asyncio.to_thread(
                        self.repository.finish, task_id, config_id, claim, fingerprint=fingerprint
                    )
                    return None if finished else 1
                transient = response.status_code in {408, 429} or response.status_code >= 500
            attempts = int(row["attempt_count"] or 0)
            delay = _BACKOFF[attempts] if transient and attempts < len(_BACKOFF) else None
            finished = await asyncio.to_thread(
                self.repository.finish,
                task_id,
                config_id,
                claim,
                fingerprint=None,
                retry_seconds=delay,
                permanent=delay is None,
            )
            if delay is not None:
                logger.warning("a2a_push_retry", extra={"task_id": task_id, "config_id": config_id})
            else:
                logger.error("a2a_push_permanent_failure", extra={"task_id": task_id, "config_id": config_id})
            return delay if finished else 1
        finally:
            await asyncio.to_thread(self.repository.release, task_id, config_id, claim)
