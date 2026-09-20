from __future__ import annotations

import json
import logging
from typing import Any

_logger = logging.getLogger("managed_agents_app")
if not _logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
_logger.setLevel(logging.INFO)


def log(level: str, event: str, **fields: Any) -> None:
    record = json.dumps({"level": level, "event": event, **fields}, separators=(",", ":"), default=str)
    getattr(_logger, level if level in {"info", "warning", "error"} else "info")(record)
