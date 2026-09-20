from typing import Any, Protocol


class EventBus(Protocol):
    def publish(self, source: str, detail_type: str, detail: Any) -> None: ...
