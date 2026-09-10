"""Meta deauthorize/delete atzvani — bez tiem Threads use case nesaglabājas."""
import base64
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app import credentials
from app.main import app


@pytest.fixture()
def client(session):
    with TestClient(app) as c:
        yield c


def signed(payload: dict, secret: str) -> str:
    def enc(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    body = enc(json.dumps(payload).encode())
    return f"{enc(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())}.{body}"


@pytest.fixture()
def connected(session, monkeypatch):
    monkeypatch.delenv("THREADS_ACCESS_TOKEN", raising=False)
    credentials.put(session, "threads_app_secret", "app-secret")
    credentials.put(session, "threads_user_id", "9", label="tv3")
    credentials.put(session, "threads_token", "tok", label="tv3")


def test_uninstall_drops_the_token(client, session, connected):
    r = client.post("/connect/threads/uninstall",
                    data={"signed_request": signed({"user_id": "9"}, "app-secret")})
    assert r.status_code == 200
    session.expire_all()
    assert credentials.get("threads_token", session) == ""


def test_delete_answers_with_code_and_clears(client, session, connected):
    r = client.post("/connect/threads/delete",
                    data={"signed_request": signed({"user_id": "9"}, "app-secret")})
    assert r.status_code == 200
    body = r.json()
    assert body["confirmation_code"] and body["url"].endswith("/connect")
    session.expire_all()
    assert credentials.get("threads_token", session) == ""


def test_forged_callback_changes_nothing(client, session, connected):
    r = client.post("/connect/threads/uninstall",
                    data={"signed_request": signed({"user_id": "9"}, "cita")})
    assert r.status_code == 400
    session.expire_all()
    assert credentials.get("threads_token", session) == "tok"
