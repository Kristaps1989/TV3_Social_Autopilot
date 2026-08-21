import json
from pathlib import Path

from app.ingest import (
    cancel_posts_for_dont,
    parse_feed_body,
    status_from_feed_url,
    upsert_article,
)
from app.models import Post

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_json_feed():
    body = (FIXTURES / "feed_sample.json").read_text(encoding="utf-8")
    items = parse_feed_body(body, "application/json", "news_all", "news",
                            {26336: "news", 3383: "sport"})
    assert len(items) == 3
    first = items[0]
    assert first["title"] == "Valdība apstiprina jauno budžetu"
    assert first["editor_status"] == "must"
    assert first["section"] == "news"
    assert first["images"] == ["https://www.tv3.lv/img/budzets.jpg"]
    sport = items[2]
    assert sport["section"] == "sport"


def test_parse_rss_feed():
    body = (FIXTURES / "feed_sample.rss").read_text(encoding="utf-8")
    items = parse_feed_body(body, "application/rss+xml", "sport_all", "sport", {})
    assert len(items) == 2
    assert items[0]["section"] == "sport"
    assert items[0]["url"].startswith("https://www.tv3.lv/")


def test_status_from_feed_url():
    assert status_from_feed_url("https://tv3.lv/api/1/social/26336,60119/now/evergreen/") == "now"
    assert status_from_feed_url("https://tv3.lv/api/1/social/1/must/evergreen/") == "must"
    assert status_from_feed_url("https://tv3.lv/api/1/social/1/2/") == ""


def test_upsert_dedup_and_status_change(session):
    data = {
        "guid": "wp-1", "url": "https://tv3.lv/a", "canonical_url": "https://tv3.lv/a",
        "title": "T", "lead": "", "categories": [], "term_ids": [], "images": [],
        "published_at": None, "editor_status": "can", "editor_timeframe": "",
        "section": "news", "feed_name": "f", "raw_json": {},
    }
    a1, created, changed = upsert_article(session, dict(data))
    assert created and not changed
    a1.decided_at = a1.first_seen_at  # pretend it was decided

    a2, created, changed = upsert_article(session, dict(data, editor_status="dont"))
    assert not created and changed
    assert a2.id == a1.id
    assert a2.decided_at is None  # status change forces re-decision


def test_dont_cancels_scheduled_posts(session):
    data = {
        "guid": "wp-2", "url": "https://tv3.lv/b", "canonical_url": "https://tv3.lv/b",
        "title": "T2", "lead": "", "categories": [], "term_ids": [], "images": [],
        "published_at": None, "editor_status": "can", "editor_timeframe": "",
        "section": "news", "feed_name": "f", "raw_json": {},
    }
    article, _, _ = upsert_article(session, data)
    session.add(Post(article_id=article.id, channel="x_test", state="scheduled"))
    session.add(Post(article_id=article.id, channel="fb_test", state="published"))
    session.flush()
    session.refresh(article)

    n = cancel_posts_for_dont(session, article)
    assert n == 1
    states = sorted(p.state for p in article.posts)
    assert states == ["cancelled", "published"]  # published stays published
