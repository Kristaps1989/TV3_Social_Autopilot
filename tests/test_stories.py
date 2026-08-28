from datetime import datetime, timedelta

from app import cards
from app.models import Article, Evaluation, Post, utcnow
from app.pipeline import publish_due, run_decisions
from app.slots import violates_diversity
from sqlalchemy import select


def test_story_html_brand_elements():
    html_doc = cards.build_story_html("Garš stāsta virsraksts ar āēī", "news",
                                      "https://tv3.lv/img.jpg")
    for text in ("Garš stāsta virsraksts ar āēī", "Lasi visu rakstā",
                 "tv3.lv", "1920", "data:image/png;base64,"):
        assert text in html_doc


def test_single_format_channel_not_blocked_by_diversity():
    rules = {"section_mix": {"window": 3, "min_distinct_sections": 2,
                             "min_distinct_formats": 2}}

    class P:
        def __init__(self, when):
            self.article = type("A", (), {"section": "news"})()
            self.format = "story"
            self.scheduled_at = when

    now = datetime(2026, 8, 20, 10, 0)
    queue = [P(now - timedelta(minutes=i)) for i in (10, 20, 30)]
    cfg = {"formats": ["story"], "sections": []}
    # same format forever is fine on a channel that only HAS one format
    assert not violates_diversity(queue, "sport", "story", now, rules, cfg)
    # but a multi-format channel still gets the requirement
    assert violates_diversity(queue, "sport", "story", now, rules,
                              {"formats": ["story", "link"], "sections": []})


def test_stories_channel_flow(session, monkeypatch):
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    monkeypatch.setattr(cards, "render_story",
                        lambda *a, **k: "data/cards/story_x.png")
    a = Article(guid="st-1", url="https://tv3.lv/st", canonical_url="https://tv3.lv/st",
                title="Stāsta raksts ar attēlu", section="news",
                editor_status="must", images=["https://tv3.lv/i.jpg"],
                published_at=utcnow() - timedelta(minutes=5))
    session.add(a)
    session.commit()
    run_decisions(session)
    story_posts = session.execute(
        select(Post).where(Post.article_id == a.id, Post.channel == "fb_stories")
    ).scalars().all()
    assert len(story_posts) == 1
    assert story_posts[0].format == "story"
    assert story_posts[0].media == ["data/cards/story_x.png"]


def test_story_without_image_blocked_with_reason(session, monkeypatch):
    monkeypatch.setattr(cards, "renderer_available", lambda: False)
    a = Article(guid="st-2", url="https://tv3.lv/st2", canonical_url="https://tv3.lv/st2",
                title="Raksts bez attēla", section="news", editor_status="must",
                images=[], published_at=utcnow() - timedelta(minutes=5))
    session.add(a)
    session.commit()
    run_decisions(session)
    story_posts = session.execute(
        select(Post).where(Post.article_id == a.id, Post.channel == "fb_stories")
    ).scalars().all()
    assert story_posts == []
    evals = session.execute(
        select(Evaluation).where(Evaluation.article_id == a.id,
                                 Evaluation.channel == "fb_stories",
                                 Evaluation.outcome == "blocked")
    ).scalars().all()
    assert any("story" in e.reason for e in evals)


def test_fb_photo_gets_link_in_first_comment(session, monkeypatch):
    calls = {}

    class FakeAdapter:
        def publish(self, *, text, link, images, fmt):
            calls["text"] = text
            calls["fmt"] = fmt
            return "fake-123"

        def comment(self, post_id, message):
            calls["comment_on"] = post_id
            calls["comment"] = message
            return "c1"

    import app.pipeline as pl

    monkeypatch.setattr(pl, "get_adapter", lambda platform: FakeAdapter())
    a = Article(guid="fc-2", url="https://tv3.lv/fc2", canonical_url="https://tv3.lv/fc2",
                title="Foto raksts", section="news", images=["https://tv3.lv/i.jpg"])
    session.add(a)
    session.flush()
    p = Post(article_id=a.id, channel="fb_tv3lv", format="photo", copy="Apraksts",
             link_url=a.canonical_url, media=["https://tv3.lv/i.jpg"],
             state="scheduled", scheduled_at=utcnow() - timedelta(minutes=1))
    session.add(p)
    session.commit()

    assert publish_due(session) == 1
    assert "utm_content" in calls["comment"]        # link went to the comment
    assert calls["comment_on"] == "fake-123"
    assert "https://" not in calls["text"]           # ...and NOT into the caption


def test_fb_uploads_image_bytes_and_clear_error_on_missing_file(monkeypatch, tmp_path):
    import adapters.facebook as fbmod
    from adapters.base import PublishError
    from adapters.facebook import FacebookPageAdapter

    # missing local file -> clear retryable error, no FB call
    import pytest as _pytest

    with _pytest.raises(PublishError) as exc:
        FacebookPageAdapter._image_bytes("data/cards/nope.png")
    assert "neeksistē" in str(exc.value)
    assert exc.value.retryable is True

    # remote image is downloaded and uploaded as bytes
    calls = {}

    class FakeResp:
        status_code = 200
        content = b"png-bytes"
        text = ""

        def json(self):
            return {"id": "p1", "post_id": "pp1"}

    monkeypatch.setattr(fbmod.httpx, "get", lambda *a, **k: FakeResp())

    def fake_post(url, data=None, files=None, timeout=None):
        calls["files"] = files
        return FakeResp()

    monkeypatch.setattr(fbmod.httpx, "post", fake_post)
    adapter = FacebookPageAdapter()
    adapter.page_id, adapter.token = "42", "tok"
    adapter._upload_photo("https://tv3.lv/img.jpg", {"published": "false"})
    assert calls["files"]["source"][1] == b"png-bytes"


def test_refresh_missing_media_regenerates_photo(session, monkeypatch):
    from app import pipeline
    from app.models import Article, Post

    monkeypatch.setattr(pipeline, "branded_photo",
                        lambda article, img, platform="": "data/cards/new.png")
    a = Article(guid="rm-1", url="https://tv3.lv/rm", canonical_url="https://tv3.lv/rm",
                title="T", section="news", images=["https://tv3.lv/i.jpg"])
    session.add(a)
    session.flush()
    p = Post(article_id=a.id, channel="fb_tv3lv", format="photo", copy="x",
             media=["data/cards/gone_after_deploy.png"], state="scheduled")
    session.add(p)
    session.commit()
    pipeline.refresh_missing_media(session, p, "facebook_page")
    assert p.media == ["data/cards/new.png"]

    # raw article URL stored as fallback -> re-rendered once the renderer works
    p2 = Post(article_id=a.id, channel="x_tv3zinas", format="photo", copy="x",
              media=["https://tv3.lv/i.jpg"], state="scheduled")
    session.add(p2)
    session.commit()
    pipeline.refresh_missing_media(session, p2, "x")
    assert p2.media == ["data/cards/new.png"]

    # story with a raw fallback is re-rendered the same way
    monkeypatch.setattr(pipeline, "story_media",
                        lambda article, img: ["data/cards/story_new.png"])
    p3 = Post(article_id=a.id, channel="fb_stories", format="story", copy="",
              media=["https://tv3.lv/i.jpg"], state="scheduled")
    session.add(p3)
    session.commit()
    pipeline.refresh_missing_media(session, p3, "facebook_page")
    assert p3.media == ["data/cards/story_new.png"]

    # renderer still down -> branded_photo returns the same raw URL, unchanged
    monkeypatch.setattr(pipeline, "branded_photo",
                        lambda article, img, platform="": "https://tv3.lv/i.jpg")
    p4 = Post(article_id=a.id, channel="fb_tv3lv", format="photo", copy="x",
              media=["https://tv3.lv/i.jpg"], state="scheduled")
    session.add(p4)
    session.commit()
    pipeline.refresh_missing_media(session, p4, "facebook_page")
    assert p4.media == ["https://tv3.lv/i.jpg"]


def test_prebranded_images_keep_their_own_headline(session, monkeypatch):
    from app import pipeline
    from app.models import Article

    a = Article(guid="pb-1", url="u", canonical_url="u", title="Virsraksts",
                section="news",
                images=["https://tv3cdn.lv/photopost/2026/abc.jpg"])
    # photo: the graphic goes out untouched
    assert pipeline.branded_photo(
        a, "https://tv3cdn.lv/photopost/2026/abc.jpg") == \
        "https://tv3cdn.lv/photopost/2026/abc.jpg"
    assert pipeline.prebranded("https://tv3cdn.lv/photopost/x.png")
    assert not pipeline.prebranded("https://tv3cdn.lv/uploads/parasts-foto.jpg")

    # story: rendered without the duplicate title plate
    captured = {}

    def fake_render(title, section, image_url, kicker="", out_dir=None,
                    with_title=True):
        captured["with_title"] = with_title
        return "data/cards/story_x.png"

    monkeypatch.setattr(pipeline.cards if hasattr(pipeline, "cards") else __import__("app.cards", fromlist=["x"]),
                        "render_story", fake_render, raising=False)
    from app import cards as cards_mod

    monkeypatch.setattr(cards_mod, "render_story", fake_render)
    monkeypatch.setattr(cards_mod, "renderer_available", lambda: True)
    out = pipeline.story_media(a, "https://tv3cdn.lv/photopost/2026/abc.jpg")
    assert out == ["data/cards/story_x.png"]
    assert captured["with_title"] is False
