"""Threads atļauju pārbaude: viens īsts lasīšanas izsaukums katrai."""
import pytest

from app import config, diagnostics
from app.models import Post, utcnow


class FakeThreads:
    platform = "threads"

    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def configured(self):
        return True

    def profile(self):
        self.calls.append("profile")
        return {"id": "9", "username": "tv3.lv"}

    def fetch_insights(self, post_id):
        self.calls.append(("insights", post_id))
        if not self.ok:
            raise RuntimeError("(#10) permission threads_manage_insights")
        return {"impressions": 120, "clicks": 0, "reactions": 4}

    def fetch_replies(self, post_id, limit=10):
        self.calls.append(("replies", post_id))
        return [{"id": "r1", "text": "Kur ir pilnais raksts?", "username": "lasitajs"}]

    def comment(self, post_id, message):
        self.calls.append(("reply", post_id, message))
        return "reply-1"


@pytest.fixture()
def fake(monkeypatch):
    # kanālu saraksts no repo, ne no lokālās rediģējamās kopijas
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    adapter = FakeThreads()
    monkeypatch.setattr("adapters.get_adapter", lambda p: adapter)
    return adapter


def _published(session):
    post = Post(article_id=1, channel="threads_tv3lv", format="photo",
                state="published", platform_post_id="p-1",
                scheduled_at=utcnow(), published_at=utcnow())
    session.add(post)
    session.flush()
    return post


def test_every_permission_gets_one_real_call(session, fake):
    _published(session)
    out = diagnostics.threads_check(session)
    assert out["checks"]["threads_basic"]["result"]["username"] == "tv3.lv"
    assert out["checks"]["threads_manage_insights"]["result"]["impressions"] == 120
    assert out["checks"]["threads_read_replies"]["result"][0]["username"] == "lasitajs"
    assert ("insights", "p-1") in fake.calls


def test_missing_permission_is_named_not_swallowed(session, fake):
    _published(session)
    fake.ok = False
    out = diagnostics.threads_check(session)
    check = out["checks"]["threads_manage_insights"]
    assert check["ok"] is False
    assert "threads_manage_insights" in check["error"]


def test_without_a_published_post_it_says_so(session, fake):
    out = diagnostics.threads_check(session)
    assert "publicē vienu" in out["error"]
    assert "threads_basic" in out["checks"]        # kontu pārbaudīt var vienmēr


def test_reply_button_writes_the_link_once(session, fake, monkeypatch):
    """Publicēšana atbildi raksta tikai ar ieslēgtu threads_link_in_reply un
    tikai media formātam. Ja ieraksts jau aizgājis bez tās, saiti zem tā
    tomēr var pielikt — bet ne divreiz."""
    from fastapi.testclient import TestClient

    from app import config
    from app.main import app

    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    monkeypatch.setattr(config, "load_channels",
                        lambda: {"threads_tv3lv": {"platform": "threads"}})
    sent = []
    fake.comment = lambda pid, text: sent.append((pid, text)) or "reply-1"

    post = _published(session)
    post.link_url = "https://tv3.lv/zinas/raksts"
    session.commit()

    client = TestClient(app)
    client.post("/setup", data={"password": "slepens123", "password2": "slepens123"})
    r = client.post("/logs/threads-reply", follow_redirects=False)
    assert r.status_code == 303 and "saved=" in r.headers["location"]
    assert sent and "tv3.lv" in sent[0][1]

    r = client.post("/logs/threads-reply", follow_redirects=False)
    assert "error=" in r.headers["location"]
    assert len(sent) == 1


def _client(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    c = TestClient(app)
    c.__enter__()
    c.post("/setup", data={"password": "slepens123", "password2": "slepens123"})
    return c


def test_preview_shows_threads_views_and_replies_with_a_reply_button(session, fake, monkeypatch):
    """threads_read_replies un threads_manage_insights lietojums saskarnē:
    tur pat, kur ierakstu apstiprināja, ne tikai diagnostikas JSON."""
    from app.models import Article

    a = Article(guid="th-prev", url="https://tv3.lv/a", canonical_url="https://tv3.lv/a",
                title="Raksts", section="news", editor_status="can",
                published_at=utcnow(), raw_json={}, feed_name="tv3")
    session.add(a)
    session.flush()
    post = _published(session)
    post.article_id, post.link_url = a.id, "https://tv3.lv/a"
    session.commit()
    c = _client(monkeypatch)
    try:
        html = c.get(f"/post/{post.id}/preview").text
        assert "120 skatījumi" in html
        assert "@lasitajs" in html and "Kur ir pilnais raksts?" in html
        assert f"/post/{post.id}/threads-reply" in html

        r = c.post(f"/post/{post.id}/threads-reply", follow_redirects=False)
        assert "ok=1" in r.headers["location"]
        session.expire_all()
        assert post.extra["threads_reply_id"]
        assert any(call[0] == "reply" for call in fake.calls)
        html = c.get(f"/post/{post.id}/preview").text
        assert "Saite uz rakstu ir atbildē" in html
        assert f"/post/{post.id}/threads-reply" not in html   # otrreiz ne
    finally:
        c.__exit__(None, None, None)


def test_accounts_page_shows_the_connected_threads_username(session, fake, monkeypatch):
    """threads_basic: GET /me lietotājvārds pie «savienots», lai redz, ka
    pieslēgts tv3.lv, ne personīgais konts."""
    from app import credentials

    credentials.put(session, "threads_user_id", "9")
    credentials.put(session, "threads_token", "tok")
    c = _client(monkeypatch)
    try:
        assert "@tv3.lv" in c.get("/connect").text
        session.expire_all()
        assert credentials.info(session, "threads_token").label == "@tv3.lv"
    finally:
        c.__exit__(None, None, None)
