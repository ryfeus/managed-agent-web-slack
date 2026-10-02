"""Durable scheduler wakeups contain controller context identity only."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class SchedulerTrigger(Protocol):
    def schedule(self, context_id: str, reason: str, delay_seconds: int = 0) -> None: ...


class LocalSchedulerTrigger:
    def __init__(self) -> None:
        self.pending: list[tuple[str, str, int]] = []

    def schedule(self, context_id: str, reason: str, delay_seconds: int = 0) -> None:
        self.pending.append((context_id, reason, delay_seconds))


class AsyncLocalSchedulerTrigger:
    """Run one durable scheduler step per in-process wakeup in the local ASGI app."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._scheduler: Any = None
        self._jobs: set[asyncio.Task[None]] = set()
        self._stopped = False

    def start(self, scheduler: Any) -> None:
        if self._loop is not None:
            raise RuntimeError("Local scheduler already started")
        self._loop = asyncio.get_running_loop()
        self._scheduler = scheduler

    def schedule(self, context_id: str, reason: str, delay_seconds: int = 0) -> None:
        if not context_id or not reason or not 0 <= delay_seconds <= 900:
            raise ValueError("Invalid scheduler wakeup")
        if self._loop is None or self._stopped:
            raise RuntimeError("Local scheduler is not running")

        def launch() -> None:
            if self._stopped:
                return
            job = asyncio.create_task(self._run(context_id, delay_seconds))
            self._jobs.add(job)
            job.add_done_callback(self._jobs.discard)

        self._loop.call_soon_threadsafe(launch)

    async def _run(self, context_id: str, delay_seconds: int) -> None:
        try:
            if delay_seconds:
                await asyncio.sleep(delay_seconds)
            await self._scheduler.run_once(context_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            from managed_agents_app.cma_controller.scheduler import SchedulerBusy

            if isinstance(error, SchedulerBusy):
                self.schedule(context_id, "lease-busy", 1)
                return
            logger.exception("local_cma_scheduler_failed", extra={"context_id": context_id})
            self.schedule(context_id, "retry-after-error", 5)

    async def stop(self) -> None:
        self._stopped = True
        for job in tuple(self._jobs):
            job.cancel()
        await asyncio.gather(*self._jobs, return_exceptions=True)
        self._jobs.clear()


class SqsSchedulerTrigger:
    def __init__(self, queue_url: str, client: Any) -> None:
        self.queue_url = queue_url
        self.client = client

    def schedule(self, context_id: str, reason: str, delay_seconds: int = 0) -> None:
        if not context_id or not reason or not 0 <= delay_seconds <= 900:
            raise ValueError("Invalid scheduler wakeup")
        self.client.send_message(
            QueueUrl=self.queue_url,
            MessageBody=json.dumps({"version": 1, "contextId": context_id, "reason": reason}),
            DelaySeconds=delay_seconds,
        )
