from __future__ import annotations

from managed_agents_app.auth import (
    MAX_AGE_SECONDS,
    clear_session_cookie,
    create_session_cookie,
    principal_from_cookie,
    verify_access_token,
)


def test_cookie_round_trip(monkeypatch) -> None:
    monkeypatch.setattr("managed_agents_app.auth.time.time", lambda: 1_000.0)
    cookie = create_session_cookie("principal-1", "secret", secure=True)
    assert principal_from_cookie(cookie, "secret", now_ms=1_000_001) == "principal-1"
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
    assert "Secure" in cookie
    assert f"Max-Age={MAX_AGE_SECONDS}" in cookie


def test_cookie_rejects_tampering_expiry_and_wrong_secret(monkeypatch) -> None:
    monkeypatch.setattr("managed_agents_app.auth.time.time", lambda: 1_000.0)
    cookie = create_session_cookie("principal-1", "secret", secure=False)
    assert principal_from_cookie(cookie, "wrong") is None
    cookie_name, cookie_value = cookie.split(";", 1)[0].split("=", 1)
    payload, signature = cookie_value.split(".", 1)
    tampered = f"{cookie_name}={payload[:-1]}A.{signature};{cookie_value}"
    assert principal_from_cookie(tampered, "secret") is None
    assert principal_from_cookie(cookie, "secret", now_ms=1_000_000 + MAX_AGE_SECONDS * 1000) is None


def test_access_token_and_clear_cookie() -> None:
    assert verify_access_token("same", "same")
    assert not verify_access_token("different", "same")
    assert clear_session_cookie(secure=False).endswith("Max-Age=0")
