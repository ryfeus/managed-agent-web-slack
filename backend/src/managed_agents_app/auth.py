from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

SESSION_COOKIE = "managed_agent_session"
MAX_AGE_SECONDS = 12 * 60 * 60


def verify_access_token(actual: str, expected: str) -> bool:
    return hmac.compare_digest(actual.encode(), expected.encode())


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(payload: str, secret: str) -> str:
    return _encode(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())


def create_session_cookie(principal_id: str, secret: str, *, secure: bool = True) -> str:
    payload = _encode(
        json.dumps(
            {"principalId": principal_id, "expiresAt": int(time.time() * 1000) + MAX_AGE_SECONDS * 1000},
            separators=(",", ":"),
        ).encode()
    )
    parts = [
        f"{SESSION_COOKIE}={payload}.{_sign(payload, secret)}",
        "Path=/",
        "HttpOnly",
        "SameSite=Strict",
    ]
    if secure:
        parts.append("Secure")
    parts.append(f"Max-Age={MAX_AGE_SECONDS}")
    return "; ".join(parts)


def clear_session_cookie(*, secure: bool = True) -> str:
    parts = [f"{SESSION_COOKIE}=", "Path=/", "HttpOnly", "SameSite=Strict"]
    if secure:
        parts.append("Secure")
    parts.append("Max-Age=0")
    return "; ".join(parts)


def principal_from_cookie(cookie_header: str | None, secret: str, *, now_ms: int | None = None) -> str | None:
    if not cookie_header:
        return None
    value = next(
        (
            part.strip()[len(SESSION_COOKIE) + 1 :]
            for part in cookie_header.split(";")
            if part.strip().startswith(f"{SESSION_COOKIE}=")
        ),
        None,
    )
    if not value or "." not in value:
        return None
    payload, signature = value.rsplit(".", 1)
    if not hmac.compare_digest(signature, _sign(payload, secret)):
        return None
    try:
        decoded: dict[str, Any] = json.loads(_decode(payload))
        principal_id = decoded.get("principalId")
        expires_at = decoded.get("expiresAt")
        current = now_ms if now_ms is not None else int(time.time() * 1000)
        if not isinstance(principal_id, str) or not isinstance(expires_at, int) or expires_at <= current:
            return None
        return principal_id
    except ValueError, TypeError, json.JSONDecodeError:
        return None
