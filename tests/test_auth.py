import pytest
from fastapi.testclient import TestClient

from app import auth, credentials
from app.main import app


@pytest.fixture()
def client(session, monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    with TestClient(app) as c:
        yield c


def test_first_run_redirects_to_setup(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    assert client.get("/setup").status_code == 200


def test_setup_creates_password_and_logs_in(client, session):
    r = client.post("/setup", data={"password": "slepens123", "password2": "slepens123"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert auth.SESSION_COOKIE in r.cookies
    assert client.get("/").status_code == 200  # cookie carried by the client
    # setup is one-time: afterwards it redirects to login
    r = client.get("/setup", follow_redirects=False)
    assert r.headers["location"] == "/login"


def test_setup_rejects_short_and_mismatched(client):
    r = client.post("/setup", data={"password": "abc", "password2": "abc"},
                    follow_redirects=False)
    assert "/setup?error=" in r.headers["location"]
    r = client.post("/setup", data={"password": "slepens123", "password2": "cits12345"},
                    follow_redirects=False)
    assert "/setup?error=" in r.headers["location"]


def test_login_and_logout(client, session):
    credentials.put(session, "admin_password_hash", auth.hash_password("slepens123"))
    r = client.get("/", follow_redirects=False)
    assert r.headers["location"] == "/login"

    r = client.post("/login", data={"password": "nepareiza"}, follow_redirects=False)
    assert "error" in r.headers["location"]

    r = client.post("/login", data={"password": "slepens123"}, follow_redirects=False)
    assert r.headers["location"] == "/"
    assert client.get("/connect").status_code == 200

    client.post("/logout", follow_redirects=False)
    r = client.get("/", follow_redirects=False)
    assert r.headers["location"] == "/login"


def test_env_password_still_accepted(client, session, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "no-vides-123")
    r = client.post("/login", data={"password": "no-vides-123"}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_health_stays_public(client):
    assert client.get("/health").status_code == 200


def test_tampered_cookie_rejected(client, session):
    credentials.put(session, "admin_password_hash", auth.hash_password("slepens123"))
    client.cookies.set(auth.SESSION_COOKIE, "admin.9999999999.deadbeef")
    r = client.get("/", follow_redirects=False)
    assert r.headers["location"] == "/login"


def test_anthropic_key_saved_and_visible(client, session, monkeypatch):
    credentials.put(session, "admin_password_hash", auth.hash_password("slepens123"))
    client.post("/login", data={"password": "slepens123"})

    class FakeMessages:
        def create(self, **kw):
            return None

    class FakeClient:
        def __init__(self, api_key):
            self.messages = FakeMessages()

    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    r = client.post("/connect/anthropic", data={"api_key": "sk-ant-test-1234"},
                    follow_redirects=False)
    assert "connected" in r.headers["location"]
    assert credentials.get("anthropic_api_key", session) == "sk-ant-test-1234"

    r = client.post("/connect/anthropic", data={"api_key": "wrong-prefix"},
                    follow_redirects=False)
    assert "error" in r.headers["location"]

    r = client.post("/connect/anthropic", data={"api_key": ""}, follow_redirects=False)
    assert credentials.get("anthropic_api_key", session) == ""
