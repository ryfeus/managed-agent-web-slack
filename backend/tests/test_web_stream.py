from __future__ import annotations

from managed_agents_app.handlers.web_stream import _STREAM_END, _next_event, app


def test_stream_app_registers_and_iterator_end_uses_sentinel() -> None:
    assert app.routes
    iterator = iter([{"id": "evt_1"}])
    assert _next_event(iterator) == {"id": "evt_1"}
    assert _next_event(iterator) is _STREAM_END
