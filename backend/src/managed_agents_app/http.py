from __future__ import annotations

import base64
import json
from typing import Any


def raw_body(event: dict[str, Any]) -> str:
    body = event.get("body") or ""
    return base64.b64decode(body).decode() if event.get("isBase64Encoded") else str(body)


def headers(event: dict[str, Any]) -> dict[str, str]:
    return {
        str(key).lower(): str(value)
        for key, value in (event.get("headers") or {}).items()
        if value is not None
    }


def response(
    status_code: int,
    body: Any,
    *,
    extra_headers: dict[str, str] | None = None,
    cookies: list[str] | None = None,
) -> dict[str, Any]:
    result_headers = {"content-type": "application/json", **(extra_headers or {})}
    if cookies:
        result_headers["set-cookie"] = cookies[0]
    return {
        "statusCode": status_code,
        "headers": result_headers,
        "body": json.dumps(body, separators=(",", ":"), default=str),
    }
