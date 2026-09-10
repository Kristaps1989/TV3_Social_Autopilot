"""Threads atļauju pārbaude: viens īsts lasīšanas izsaukums katrai."""
import pytest

from app import diagnostics
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


@pytest.fixture()
def fake(monkeypatch):
    adapter = FakeThreads()
    monkeypatch.setattr("adapters.get_adapter", lambda p: adapter)
    return adapter


def _published(session):
    post = Post(article_id=1, channel="threads_sport", format="photo",
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
