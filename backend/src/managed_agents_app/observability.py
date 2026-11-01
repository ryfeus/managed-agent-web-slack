"""Content-free timings for blocking work and durable workflow reconciliation."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
from time import monotonic
from typing import Any
from uuid import uuid4

_task_id: ContextVar[str | None] = ContextVar("timing_task_id", default=None)
_active: Counter[str] = Counter()
_lock = Lock()
_logger = logging.getLogger("managed_agents_app.timing")
if not _logger.handlers:
    _logger.addHandler(logging.StreamHandler())
_logger.setLevel(logging.INFO)
_logger.propagate = False


def emit(event: str, **metadata: Any) -> None:
    _logger.info(json.dumps({"event": event, "task_id": _task_id.get(), **metadata}, separators=(",", ":")))


@contextmanager
def task_scope(task_id: str) -> Iterator[None]:
    token = _task_id.set(task_id)
    try:
        yield
    finally:
        _task_id.reset(token)


async def blocking[T](operation: str, call: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Distinguish executor queue wait from execution, including cancelled readers.

    Arguments, return values and exception messages are deliberately never logged.
    A worker's finish is separate from its waiter's cancellation: cancelling a
    to_thread waiter does not stop an already-running synchronous operation.
    """
    span_id = uuid4().hex
    queued = monotonic()
    loop = asyncio.get_running_loop()
    emit("blocking_queued", operation=operation, span_id=span_id)

    def run() -> T:
        started = monotonic()
        with _lock:
            _active[operation] += 1
            active = dict(+_active)
        executor = getattr(loop, "_default_executor", None)
        emit(
            "blocking_started",
            operation=operation,
            span_id=span_id,
            queue_ms=round((started - queued) * 1000, 3),
            active_calls=active,
            executor_workers=getattr(executor, "_max_workers", None),
        )
        outcome = "success"
        error_type = None
        try:
            return call(*args, **kwargs)
        except BaseException as error:
            outcome, error_type = "error", type(error).__name__
            raise
        finally:
            with _lock:
                _active[operation] -= 1
                active = dict(+_active)
            emit(
                "blocking_finished",
                operation=operation,
                span_id=span_id,
                execution_ms=round((monotonic() - started) * 1000, 3),
                outcome=outcome,
                error_type=error_type,
                active_calls=active,
            )

    try:
        return await asyncio.to_thread(run)
    except asyncio.CancelledError:
        emit("blocking_waiter_cancelled", operation=operation, span_id=span_id)
        raise
