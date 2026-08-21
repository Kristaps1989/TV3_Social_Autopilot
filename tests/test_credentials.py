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
