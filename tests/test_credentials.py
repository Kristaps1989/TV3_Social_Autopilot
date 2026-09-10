from datetime import timedelta

from app import credentials
from app.models import utcnow


def test_db_wins_over_env(session, monkeypatch):
    monkeypatch.setenv("FB_PAGE_ID", "env-page")
    assert credentials.get("fb_page_id", session) == "env-page"
    credentials.put(session, "fb_page_id", "db-page", label="tv3.lv")
    assert credentials.get("fb_page_id", session) == "db-page"


def test_oauth_state_single_use(session):
    state = credentials.new_state(session)
    assert credentials.check_state(session, state) is True
    assert credentials.check_state(session, state) is False  # burned
    assert credentials.check_state(session, "wrong") is False


def test_connection_status(session, monkeypatch):
    for var in ("FB_PAGE_ID", "FB_PAGE_ACCESS_TOKEN", "THREADS_USER_ID",
                "THREADS_ACCESS_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    status = credentials.connection_status(session)
    assert status["facebook"]["connected"] is False
    credentials.put(session, "fb_page_id", "1", label="tv3.lv")
    credentials.put(session, "fb_page_token", "tok", label="tv3.lv")
    status = credentials.connection_status(session)
    assert status["facebook"]["connected"] is True
    assert status["facebook"]["source"] == "admin"
    assert status["facebook"]["label"] == "tv3.lv"


def test_expiry_warning(session, monkeypatch):
    monkeypatch.setattr(credentials, "refresh_threads_token", lambda s: False)
    credentials.put(session, "threads_token", "tok",
                    expires_at=utcnow() + timedelta(days=3))
    warnings = credentials.maintain_tokens(session)
    assert warnings and "Threads" in warnings[0]


def _signed(payload: dict, secret: str) -> str:
    import base64
    import hashlib
    import hmac
    import json

    def enc(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    body = enc(json.dumps(payload).encode())
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{enc(sig)}.{body}"


def test_signed_request_checks_signature():
    import pytest

    signed = _signed({"user_id": "42"}, "secret")
    assert credentials.parse_signed_request(signed, "secret")["user_id"] == "42"
    with pytest.raises(ValueError):
        credentials.parse_signed_request(signed, "cita-atslega")
    with pytest.raises(ValueError):
        credentials.parse_signed_request("nav-punkta", "secret")


def test_threads_scopes_cover_replies():
    url = credentials.threads_auth_url("https://x.test/cb", "st")
    assert "threads_content_publish" in url
    assert "threads_manage_replies" in url
