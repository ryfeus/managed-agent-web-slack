from __future__ import annotations

from functools import lru_cache
from typing import Any

import boto3

from managed_agents_app.event_wire import serialize_event_detail


@lru_cache(maxsize=1)
def _client() -> Any:
    return boto3.client("events")


def put_domain_event(bus_name: str, source: str, detail_type: str, detail: Any) -> None:
    response = _client().put_events(
        Entries=[
            {
                "EventBusName": bus_name,
                "Source": source,
                "DetailType": detail_type,
                "Detail": serialize_event_detail(detail),
            }
        ]
    )
    if response.get("FailedEntryCount"):
        raise RuntimeError(f"EventBridge rejected {response['FailedEntryCount']} event(s)")


class EventBridgeEventBus:
    def __init__(self, bus_name: str) -> None:
        self.bus_name = bus_name

    def publish(self, source: str, detail_type: str, detail: Any) -> None:
        put_domain_event(self.bus_name, source, detail_type, detail)
