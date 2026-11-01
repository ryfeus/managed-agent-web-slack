"""Controller-only push wakeups."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class PushTrigger(Protocol):
    def schedule_context(self, context_id: str, reason: str, delay_seconds: int = 0) -> None: ...


class LocalPushTrigger:
    def __init__(self) -> None:
        self.pending: list[tuple[str, str, int]] = []

    def schedule_context(self, context_id: str, reason: str, delay_seconds: int = 0) -> None:
        if not context_id or not reason or not 0 <= delay_seconds <= 900:
            raise ValueError("Invalid push wakeup")
        self.pending.append((context_id, reason, delay_seconds))


class AsyncLocalPushTrigger:
    """Local queue and delayed retries, with at most one queued wakeup per context."""

    def __init__(self) -> None:
        self.pending: list[tuple[str, str, int]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[str] | None = None
        self._queued: set[str] = set()
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._worker: asyncio.Task[None] | None = None
        self._dispatcher: Any = None

    def start(self, dispatcher: Any) -> None:
        if self._worker is not None:
            raise RuntimeError("Local push worker already started")
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._dispatcher = dispatcher
        for context_id, _, delay in self.pending:
            self._enqueue(context_id, delay)
        self.pending.clear()
        self._worker = asyncio.create_task(self._run())

    def _enqueue(self, context_id: str, delay_seconds: int = 0) -> None:
        if self._queue is None or self._loop is None:
            return
        timer = self._timers.get(context_id)
        if delay_seconds:
            if timer and timer.when() <= self._loop.time() + delay_seconds:
                return
            if timer:
                timer.cancel()
            self._timers[context_id] = self._loop.call_later(delay_seconds, self._enqueue, context_id)
            return
        if timer:
            timer.cancel()
            self._timers.pop(context_id)
        if context_id not in self._queued:
            self._queued.add(context_id)
            self._queue.put_nowait(context_id)

    def schedule_context(self, context_id: str, reason: str, delay_seconds: int = 0) -> None:
        if not context_id or not reason or not 0 <= delay_seconds <= 900:
            raise ValueError("Invalid push wakeup")
        if self._loop is None:
            self.pending.append((context_id, reason, delay_seconds))
        else:
            self._loop.call_soon_threadsafe(self._enqueue, context_id, delay_seconds)

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            context_id = await self._queue.get()
            # A state change during dispatch must enqueue another read, not get lost.
            self._queued.discard(context_id)
            try:
                await self._dispatcher.run_context(context_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("local_a2a_push_worker_failed", extra={"context_id": context_id})
            finally:
                self._queue.task_done()

    async def stop(self) -> None:
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        self._worker = None
        self._queue = None
        self._loop = None
        self._queued.clear()


class SqsPushTrigger:
    def __init__(self, queue_url: str, client: Any) -> None:
        self.queue_url = queue_url
        self.client = client

    def schedule_context(self, context_id: str, reason: str, delay_seconds: int = 0) -> None:
        if not context_id or not reason or not 0 <= delay_seconds <= 900:
            raise ValueError("Invalid push wakeup")
        self.client.send_message(
            QueueUrl=self.queue_url,
            DelaySeconds=delay_seconds,
            MessageBody=json.dumps({"version": 1, "contextId": context_id, "reason": reason}),
        )
