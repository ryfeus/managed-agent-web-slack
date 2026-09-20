from __future__ import annotations

import json
from typing import Any


def normalize_event_detail(detail: Any) -> Any:
    """Return the value that survives the production JSON event boundary."""
    boundary = getattr(detail, "boundary", None)
    if callable(boundary):
        detail = boundary()
    encoded = json.dumps(detail, separators=(",", ":"), allow_nan=False)
    return json.loads(encoded)


def serialize_event_detail(detail: Any) -> str:
    return json.dumps(normalize_event_detail(detail), separators=(",", ":"), allow_nan=False)
