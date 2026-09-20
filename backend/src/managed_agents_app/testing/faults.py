from __future__ import annotations

from threading import Event, RLock
from typing import Any


class FaultInjector:
    def __init__(self) -> None:
        self.lock = RLock()
        self.rules: list[dict[str, Any]] = []

    def arm(self, point: str, key: str = "", behavior: str = "raise", times: int = 1) -> None:
        if behavior not in {"raise", "pause"}:
            raise ValueError("Fault behavior must be raise or pause")
        with self.lock:
            self.rules.append(
                {
                    "point": point,
                    "key": key,
                    "behavior": behavior,
                    "remaining": times,
                    "entered": 0,
                    "gate": Event(),
                }
            )

    def hit(self, point: str, key: str = "") -> None:
        with self.lock:
            rule = next(
                (
                    r
                    for r in self.rules
                    if r["point"] == point and (not r["key"] or r["key"] == key) and r["remaining"] > 0
                ),
                None,
            )
            if rule is None:
                return
            rule["remaining"] -= 1
            rule["entered"] += 1
        if rule["behavior"] == "raise":
            raise RuntimeError(f"Injected fault: {point}")
        if not rule["gate"].wait(20):
            raise RuntimeError(f"Fault barrier timed out: {point}")

    def release(self, point: str, key: str = "") -> None:
        with self.lock:
            for rule in self.rules:
                if rule["point"] == point and rule["key"] == key:
                    rule["gate"].set()

    def reset(self) -> None:
        with self.lock:
            for rule in self.rules:
                rule["gate"].set()
            self.rules.clear()

    def snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            return [{k: v for k, v in r.items() if k != "gate"} for r in self.rules]
