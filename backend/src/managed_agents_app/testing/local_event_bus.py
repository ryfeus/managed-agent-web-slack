from __future__ import annotations

from collections import deque
from collections.abc import Callable
from copy import deepcopy
from threading import Lock, RLock, Thread
from typing import Any

from managed_agents_app.event_wire import normalize_event_detail


class LocalEventBus:
    def __init__(self, auto_drain: bool = False) -> None:
        self.after_delivery: Callable[[], None] = lambda: None
        self.auto_drain = auto_drain
        self.dispatch: Callable[[dict[str, Any]], None] | None = None
        self.lock = RLock()
        self.drain_lock = Lock()
        self.queue: deque[dict[str, Any]] = deque()
        self.history: list[dict[str, Any]] = []
        self.failures: dict[str, int] = {}
        self.counter = 0
        self.agent_input_queue: Any | None = None

    def publish(self, source: str, detail_type: str, detail: Any) -> None:
        detail = normalize_event_detail(detail)
        with self.lock:
            self.counter += 1
            self.queue.append(
                {
                    "id": self.counter,
                    "source": source,
                    "detail-type": detail_type,
                    "detail": deepcopy(detail),
                    "attempt": 0,
                }
            )
        if self.auto_drain:
            Thread(target=self.drain, daemon=True).start()

    def _deliver(self, event: dict[str, Any]) -> None:
        with self.lock:
            event["attempt"] += 1
            record = {**deepcopy(event), "status": "running"}
            self.history.append(record)
        try:
            kind = event["detail-type"]
            with self.lock:
                if self.failures.get(kind, 0):
                    self.failures[kind] -= 1
                    raise RuntimeError(f"Injected delivery failure: {kind}")
            if self.dispatch is None:
                raise RuntimeError("Local event router is not configured")
            self.dispatch(deepcopy(event))
            record["status"] = "completed"
            self.after_delivery()
        except Exception as error:
            record.update(status="failed", error=str(error))

    def drain(self, max_events: int = 100, workers: int = 1) -> dict[str, Any]:
        from concurrent.futures import ThreadPoolExecutor

        with self.drain_lock, ThreadPoolExecutor(max_workers=workers) as pool:
            processed = 0
            while processed < max_events:
                with self.lock:
                    batch = [
                        self.queue.popleft()
                        for _ in range(min(workers, len(self.queue), max_events - processed))
                    ]
                if not batch:
                    break
                futures = [pool.submit(self._deliver, event) for event in batch]
                for future in futures:
                    future.result()
                processed += len(batch)
        return self.snapshot()

    def retry(self, event_id: int) -> None:
        with self.lock:
            record = next(item for item in reversed(self.history) if item["id"] == event_id)
            if record["status"] != "failed":
                raise ValueError("Only failed deliveries can be retried")
            self.queue.append(
                {k: deepcopy(record[k]) for k in ["id", "source", "detail-type", "detail", "attempt"]}
            )
            record["status"] = "retried"

    @property
    def pending_count(self) -> int:
        with self.lock:
            return len(self.queue)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return deepcopy({"pending": list(self.queue), "history": self.history})

    def reset(self) -> None:
        if not self.drain_lock.acquire(timeout=12):
            raise RuntimeError("Event delivery did not stop during reset")
        try:
            with self.lock:
                self.queue.clear()
                self.history.clear()
                self.failures.clear()
                self.counter = 0
        finally:
            self.drain_lock.release()
