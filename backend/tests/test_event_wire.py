from datetime import date, datetime
from uuid import UUID

import pytest

from managed_agents_app.domain import BoundaryModel
from managed_agents_app.event_wire import normalize_event_detail, serialize_event_detail
from managed_agents_app.testing.local_event_bus import LocalEventBus


class TypedDetail(BoundaryModel):
    identifier: UUID
    created_on: date
    observed_at: datetime


def test_nested_json_values_survive_wire_boundary_and_are_independent():
    original = {"nested": [{"text": "ok", "count": 2, "active": True, "empty": None}]}
    normalized = normalize_event_detail(original)
    original["nested"][0]["text"] = "changed"
    assert normalized == {"nested": [{"text": "ok", "count": 2, "active": True, "empty": None}]}
    assert serialize_event_detail(normalized) == (
        '{"nested":[{"text":"ok","count":2,"active":true,"empty":null}]}'
    )


def test_boundary_models_render_uuid_and_dates_as_json_values():
    detail = TypedDetail(
        identifier=UUID("00000000-0000-4000-8000-000000000001"),
        created_on=date(2026, 9, 6),
        observed_at=datetime(2026, 9, 6, 12, 30),
    )
    assert normalize_event_detail(detail) == {
        "identifier": "00000000-0000-4000-8000-000000000001",
        "created_on": "2026-09-06",
        "observed_at": "2026-09-06T12:30:00",
    }


@pytest.mark.parametrize("value", [{"bad": object()}, {"bad": float("nan")}])
def test_unsupported_values_fail_instead_of_being_coerced(value):
    with pytest.raises((TypeError, ValueError)):
        normalize_event_detail(value)


def test_local_bus_preserves_source_and_detail_type_across_wire_boundary():
    bus = LocalEventBus()
    bus.publish("app.test", "TestDetail", {"value": [1, 2]})
    event = bus.snapshot()["pending"][0]
    assert event["source"] == "app.test"
    assert event["detail-type"] == "TestDetail"
    assert event["detail"] == {"value": [1, 2]}
