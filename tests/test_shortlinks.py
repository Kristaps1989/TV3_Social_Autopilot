from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import shortlinks
from app.main import app
from app.models import Article, Post, utcnow


@pytest.fixture()
def client(session):
    with TestClient(app) as c:
        yield c


def test_code_roundtrip_and_shape():
    for post_id in (1, 7, 78, 1234, 99999):
        code = shortlinks.encode(post_id)
        assert shortlinks.decode(code) == post_id
        assert len(code) >= 3                       # nekad neatklāj "ieraksts nr. 1"
        assert not set(code) & set("01loi")         # bez pārprotamām zīmēm
    assert shortlinks.decode("ne-derīgs") is None
    assert shortlinks.decode("") is None


def test_display_link_disabled_by_default():
    full = "https://tv3.lv/zinas/raksts?utm_source=facebook_page&utm_content=78"
    assert shortlinks.display_link(78, full, {}) == full
    assert shortlinks.short_url(78, {}) == ""
    # tukšai saitei nav ko rādīt
    assert shortlinks.display_link(78, "", {"short_link_base": "https://tv3.lv/r"}) == ""


def test_display_link_uses_configured_base():
    rules = {"short_link_base": "https://tv3.lv/r/"}
    assert shortlinks.display_link(78, "https://tv3.lv/a?utm_content=78", rules) == \
        f"https://tv3.lv/r/{shortlinks.encode(78)}"


def test_bot_user_agents_are_not_counted():
    assert shortlinks.is_bot("facebookexternalhit/1.1")
    assert shortlinks.is_bot("")
    assert not shortlinks.is_bot(
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Safari/605.1")


def _post(session, **kw):
    a = Article(guid="sl-a", url="https://tv3.lv/a", canonical_url="https://tv3.lv/a",
                title="T", section="news")
    session.add(a)
    session.flush()
    p = Post(article_id=a.id, channel="fb_tv3lv", format="photo", copy="Teksts",
             link_url=a.canonical_url, state="scheduled", **kw)
    session.add(p)
    session.commit()
    return p


def test_redirect_carries_utm_and_counts_clicks(client, session):
    post = _post(session, hook_type="number")
    resp = client.get(f"/r/{shortlinks.encode(post.id)}", follow_redirects=False,
                      headers={"user-agent": "Mozilla/5.0 (iPhone) Safari"})
    assert resp.status_code == 302
    target = resp.headers["location"]
    assert target.startswith("https://tv3.lv/a?")
    assert f"utm_content={post.id}" in target
    assert "utm_term=number" in target
    assert "utm_source=facebook_page" in target
    session.expire_all()
    assert session.get(Post, post.id).short_hits == 1

    # platformas pievienotie parametri (fbclid) ceļo līdzi, boti neskaitās
    resp = client.get(f"/r/{shortlinks.encode(post.id)}?fbclid=abc",
                      follow_redirects=False,
                      headers={"user-agent": "facebookexternalhit/1.1"})
    assert "fbclid=abc" in resp.headers["location"]
    session.expire_all()
    assert session.get(Post, post.id).short_hits == 1


def test_unknown_code_lands_on_the_portal(client):
    resp = client.get("/r/zzzzz", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://tv3.lv/"
    # nederīgs kods arī neuzmet kļūdu lasītājam
    resp = client.get("/r/!!", follow_redirects=False)
    assert resp.headers["location"] == "https://tv3.lv/"


def test_published_text_uses_short_link(session, monkeypatch):
    import app.pipeline as pl
    from app import config

    calls = {}

    class FakeAdapter:
        def publish(self, *, text, link, images, fmt):
            calls["text"] = text
            calls["link"] = link
            return "fb-1"

        def comment(self, post_id, message):
            calls["comment"] = message
            return "c1"

    rules = dict(config.load_rules())
    rules["short_link_base"] = "https://tv3.lv/r"
    monkeypatch.setattr(config, "load_rules", lambda: rules)
    monkeypatch.setattr(pl, "get_adapter", lambda platform: FakeAdapter())
    post = _post(session, scheduled_at=utcnow() - timedelta(minutes=1))
    short = f"https://tv3.lv/r/{shortlinks.encode(post.id)}"

    assert pl.publish_due(session) == 1
    # lasītājs redz īso saiti, API mērķis paliek pilnā adrese ar UTM
    assert calls["comment"] == short
    assert "utm_content" in calls["link"]
    assert "utm_content" not in calls["text"]
