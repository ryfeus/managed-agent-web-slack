from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Lock, RLock
from typing import Any


class LocalQueue:
    """A deterministic subset of Standard SQS semantics for local semantic E2E."""

    def __init__(
        self,
        handler: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        visibility_timeout: int = 360,
        max_receive_count: int = 5,
    ) -> None:
        self.handler = handler
        self.visibility_timeout = visibility_timeout
        self.max_receive_count = max_receive_count
        self.after_delivery: Callable[[], None] = lambda: None
        self.lock = RLock()
        self.drain_lock = Lock()
        self.now = 0
        self.counter = 0
        self.messages: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []

    def send(self, event: dict[str, Any]) -> str:
        return self.send_raw(json.dumps(event, separators=(",", ":"), allow_nan=False))

    def send_raw(self, body: str) -> str:
        with self.lock:
            self.counter += 1
            message_id = f"local-sqs-{self.counter}"
            self.messages.append(
                {
                    "id": message_id,
                    "body": body,
                    "receive_count": 0,
                    "visible_at": self.now,
                    "state": "visible",
                }
            )
            return message_id

    def duplicate(self, message_id: str) -> str:
        with self.lock:
            original = next((item for item in self.messages if item["id"] == message_id), None)
            if original is None:
                raise ValueError(f"Unknown queue message: {message_id}")
            body = str(original["body"])
        return self.send_raw(body)

    def advance_time(self, seconds: int) -> dict[str, Any]:
        if seconds < 0:
            raise ValueError("Logical time cannot move backwards")
        with self.lock:
            self.now += seconds
            self._refresh_visibility()
        return self.snapshot()

    def _refresh_visibility(self) -> None:
        for message in self.messages:
            if message["state"] == "inflight" and message["visible_at"] <= self.now:
                message["state"] = "visible"

    def _receive_batch(self, size: int) -> tuple[list[dict[str, Any]], bool]:
        with self.lock:
            self._refresh_visibility()
            received: list[dict[str, Any]] = []
            moved_to_dlq = False
            for message in self.messages:
                if len(received) >= size:
                    break
                if message["state"] != "visible":
                    continue
                if message["receive_count"] >= self.max_receive_count:
                    message["state"] = "dlq"
                    self.history.append(
                        {
                            "message_id": message["id"],
                            "receive_count": message["receive_count"],
                            "status": "dlq",
                        }
                    )
                    moved_to_dlq = True
                    continue
                message["receive_count"] += 1
                message["state"] = "inflight"
                message["visible_at"] = self.now + self.visibility_timeout
                received.append(deepcopy(message))
            return received, moved_to_dlq

    def _deliver(self, message: dict[str, Any]) -> None:
        record = {
            "messageId": message["id"],
            "body": message["body"],
            "attributes": {"ApproximateReceiveCount": str(message["receive_count"])},
        }
        error: str | None = None
        failed = False
        try:
            response = self.handler({"Records": [record]})
            failures = response.get("batchItemFailures", [])
            failed = any(item.get("itemIdentifier") == message["id"] for item in failures)
        except Exception as caught:
            failed = True
            error = str(caught)
        with self.lock:
            current = next(item for item in self.messages if item["id"] == message["id"])
            current["state"] = "inflight" if failed else "deleted"
            history = {
                "message_id": message["id"],
                "receive_count": message["receive_count"],
                "status": "failed" if failed else "completed",
            }
            if error:
                history["error"] = error
            self.history.append(history)
        if not failed:
            self.after_delivery()

    def drain(self, max_messages: int = 100, workers: int = 1) -> dict[str, Any]:
        with self.drain_lock, ThreadPoolExecutor(max_workers=workers) as pool:
            processed = 0
            while processed < max_messages:
                batch, moved_to_dlq = self._receive_batch(min(workers, max_messages - processed))
                if not batch:
                    if moved_to_dlq:
                        continue
                    break
                futures = [pool.submit(self._deliver, message) for message in batch]
                for future in futures:
                    future.result()
                processed += len(batch)
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            self._refresh_visibility()
            return deepcopy(
                {
                    "now": self.now,
                    "messages": [
                        {key: message[key] for key in ("id", "receive_count", "visible_at", "state")}
                        for message in self.messages
                    ],
                    "history": self.history,
                }
            )

    def reset(self) -> None:
        if not self.drain_lock.acquire(timeout=12):
            raise RuntimeError("Queue delivery did not stop during reset")
        try:
            with self.lock:
                self.now = 0
                self.counter = 0
                self.messages.clear()
                self.history.clear()
        finally:
            self.drain_lock.release()
