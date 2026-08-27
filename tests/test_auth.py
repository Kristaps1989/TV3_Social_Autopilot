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


def test_stats_and_preview_pages(client, session):
    credentials.put(session, "admin_password_hash", auth.hash_password("slepens123"))
    client.post("/login", data={"password": "slepens123"})
    assert client.get("/stats").status_code == 200

    from app.models import Article, Post

    a = Article(guid="ui-1", url="https://tv3.lv/u", canonical_url="https://tv3.lv/u",
                title="Priekšskatījuma tests", section="news",
                images=["https://tv3.lv/img.jpg"])
    session.add(a)
    session.flush()
    p = Post(article_id=a.id, channel="fb_tv3lv", format="link",
             copy="Teksts", link_url="https://tv3.lv/u", state="scheduled")
    session.add(p)
    session.commit()
    r = client.get(f"/post/{p.id}/preview")
    assert r.status_code == 200
    assert "Priekšskatījuma tests" in r.text
    assert "utm_content" in r.text  # full outgoing text shown with tracking


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


def test_meta_app_credentials_saved_via_ui(client, session, monkeypatch):
    for var in ("META_APP_ID", "META_APP_SECRET", "META_LOGIN_CONFIG_ID"):
        monkeypatch.delenv(var, raising=False)
    credentials.put(session, "admin_password_hash", auth.hash_password("slepens123"))
    client.post("/login", data={"password": "slepens123"})

    r = client.post("/connect/meta", data={
        "app_id": "1014675528221260", "app_secret": "sec-x",
        "config_id": "1054313277453272"}, follow_redirects=False)
    assert "connected" in r.headers["location"]
    assert credentials.fb_app() == ("1014675528221260", "sec-x")
    assert "config_id=1054313277453272" in credentials.fb_auth_url("https://x/cb", "st")

    # empty fields keep existing values
    client.post("/connect/meta", data={"app_id": "", "app_secret": "", "config_id": ""})
    assert credentials.fb_app() == ("1014675528221260", "sec-x")

    # connect page now shows the connect button
    r = client.get("/connect")
    assert "Savienot ar Facebook" in r.text


def test_disconnect_clears_page_connection(client, session, monkeypatch):
    for var in ("FB_PAGE_ID", "FB_PAGE_ACCESS_TOKEN",
                "THREADS_USER_ID", "THREADS_ACCESS_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    credentials.put(session, "admin_password_hash", auth.hash_password("slepens123"))
    client.post("/login", data={"password": "slepens123"})
    credentials.put(session, "fb_page_id", "520279391805002", label="Skatieslv")
    credentials.put(session, "fb_page_token", "tok-x", label="Skatieslv")
    credentials.put(session, "threads_user_id", "77", label="tv3sports")
    credentials.put(session, "threads_token", "tok-t")

    r = client.post("/connect/facebook/disconnect", follow_redirects=False)
    assert "connected" in r.headers["location"]
    assert credentials.get("fb_page_id", session) == ""
    assert credentials.get("fb_page_token", session) == ""
    # threads untouched by the FB disconnect
    assert credentials.get("threads_token", session) == "tok-t"

    r = client.post("/connect/threads/disconnect", follow_redirects=False)
    assert "connected" in r.headers["location"]
    assert credentials.get("threads_token", session) == ""


def test_x_keys_saved_via_ui(client, session, monkeypatch):
    for var in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"):
        monkeypatch.delenv(var, raising=False)
    credentials.put(session, "admin_password_hash", auth.hash_password("slepens123"))
    client.post("/login", data={"password": "slepens123"})

    r = client.post("/connect/x", data={
        "api_key": "k1", "api_secret": "k2",
        "access_token": "k3", "access_secret": "k4"}, follow_redirects=False)
    assert "connected" in r.headers["location"]
    assert credentials.get("x_access_token", session) == "k3"

    # empty fields keep existing values
    client.post("/connect/x", data={"api_key": "", "api_secret": "new-secret",
                                    "access_token": "", "access_secret": ""})
    assert credentials.get("x_api_key", session) == "k1"
    assert credentials.get("x_api_secret", session) == "new-secret"

    r = client.post("/connect/x/disconnect", follow_redirects=False)
    assert "connected" in r.headers["location"]
    assert credentials.get("x_api_key", session) == ""


def test_instagram_link_via_page_token(client, session, monkeypatch):
    from app import credentials as creds_mod

    for var in ("IG_USER_ID", "FB_PAGE_ID", "FB_PAGE_ACCESS_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    credentials.put(session, "admin_password_hash", auth.hash_password("slepens123"))
    client.post("/login", data={"password": "slepens123"})

    # no FB connection yet -> clear error
    r = client.post("/connect/instagram/link", follow_redirects=False)
    assert "error" in r.headers["location"]

    monkeypatch.setattr(creds_mod, "fb_page_instagram",
                        lambda s: ("17841400000000", "tv3.lv"))
    r = client.post("/connect/instagram/link", follow_redirects=False)
    assert "connected" in r.headers["location"]
    assert credentials.get("ig_user_id", session) == "17841400000000"
    assert credentials.connection_status(session)["instagram"]["label"] == "tv3.lv"

    r = client.post("/connect/instagram/disconnect", follow_redirects=False)
    assert credentials.get("ig_user_id", session) == ""


def test_inactive_channels_hidden(monkeypatch):
    from app import config

    monkeypatch.setattr(config, "_load_yaml", lambda p: {
        "fb": {"platform": "facebook_page"},
        "ig": {"platform": "instagram", "active": False},
        "on": {"platform": "x", "active": True},
    })
    channels = config.load_channels()
    assert set(channels) == {"fb", "on"}


def test_ga4_settings_saved_via_ui(client, session, monkeypatch):
    import json

    from app import ga4

    monkeypatch.delenv("GA4_PROPERTY_ID", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    credentials.put(session, "admin_password_hash", auth.hash_password("slepens123"))
    client.post("/login", data={"password": "slepens123"})

    sa = json.dumps({"type": "service_account", "client_email": "ga@x.iam",
                     "private_key": "-----BEGIN PRIVATE KEY-----..."})
    r = client.post("/connect/ga4", data={"property_id": "123456789",
                                          "service_account": sa},
                    follow_redirects=False)
    assert "connected" in r.headers["location"]
    assert ga4.property_id() == "123456789"
    assert ga4.sa_info()["client_email"] == "ga@x.iam"
    assert ga4.configured()

    # invalid JSON is rejected, existing value kept
    r = client.post("/connect/ga4", data={"property_id": "",
                                          "service_account": "{oops"},
                    follow_redirects=False)
    assert "error" in r.headers["location"]
    assert ga4.configured()

    r = client.post("/connect/ga4/disconnect", follow_redirects=False)
    assert "connected" in r.headers["location"]
    assert not ga4.configured()


def test_live_mode_toggle(client, session):
    credentials.put(session, "admin_password_hash", auth.hash_password("slepens123"))
    client.post("/login", data={"password": "slepens123"})

    assert client.get("/health").json()["dry_run"] is True  # env default
    client.post("/toggle/live")
    assert client.get("/health").json()["dry_run"] is False
    client.post("/toggle/live")
    assert client.get("/health").json()["dry_run"] is True


def test_republish_clones_dry_run_post(client, session):
    from datetime import timedelta

    from app.models import Article, Post, utcnow
    from sqlalchemy import select

    credentials.put(session, "admin_password_hash", auth.hash_password("slepens123"))
    client.post("/login", data={"password": "slepens123"})

    a = Article(guid="rp-1", url="https://tv3.lv/rp", canonical_url="https://tv3.lv/rp",
                title="Republish tests", section="news")
    session.add(a)
    session.flush()
    p = Post(article_id=a.id, channel="fb_tv3lv", format="link", copy="Teksts",
             link_url=a.canonical_url, state="published", dry_run=True,
             published_at=utcnow() - timedelta(minutes=5))
    session.add(p)
    session.commit()

    client.post(f"/post/{p.id}/republish")
    clones = session.execute(
        select(Post).where(Post.article_id == a.id, Post.state == "scheduled")
    ).scalars().all()
    assert len(clones) == 1
    assert clones[0].copy == "Teksts"

    # a REAL published post can not be re-queued this way
    p2 = Post(article_id=a.id, channel="x_tv3zinas", format="link", copy="x",
              state="published", dry_run=False, published_at=utcnow())
    session.add(p2)
    session.commit()
    client.post(f"/post/{p2.id}/republish")
    clones2 = session.execute(
        select(Post).where(Post.channel == "x_tv3zinas", Post.state == "scheduled")
    ).scalars().all()
    assert clones2 == []
