"""Digest ieraksti («TOP 5», «Nedēļa 30 sekundēs», «Trešdienas jautājums»)
sola konkrētus stāstus — un lasītājam tie jāatrod ar vienu pieskārienu."""
from datetime import datetime, timedelta

from sqlalchemy import select

from app import cards, pipeline, weekend
from app.models import Article, Post, PostMetrics, utcnow


def _article(session, guid, title, sessions, images=None, when=None):
    a = Article(guid=guid, url=f"https://tv3.lv/{guid}", canonical_url=f"https://tv3.lv/{guid}",
                title=title, section="news", ai_score=0.8,
                images=[f"https://cdn/{guid}.jpg"] if images is None else images,
                published_at=when or utcnow() - timedelta(days=1))
    session.add(a)
    session.flush()
    p = Post(article_id=a.id, channel="fb_tv3lv", format="link", copy=title,
             link_url=a.canonical_url, state="published",
             published_at=when or utcnow() - timedelta(hours=2))
    session.add(p)
    session.flush()
    session.add(PostMetrics(post_id=p.id, ga_sessions=sessions, collected_at=utcnow()))
    session.commit()
    return a


def _digest_post(session, sessions=9000):
    """Mūsu pašu «Nedēļas nogales TOP 5» ieraksts, kas plūsmā bija lasītākais."""
    a = Article(guid="digest-monday-x", url="https://tv3.lv", canonical_url="https://tv3.lv",
                title="Nedēļas nogales TOP 5", section="news", raw_json={"_digest": True},
                images=[], published_at=utcnow() - timedelta(days=1))
    session.add(a)
    session.flush()
    p = Post(article_id=a.id, channel="fb_tv3lv", format="card_carousel", copy="TOP 5",
             link_url="https://tv3.lv", state="published", hook_type="mondaytop5",
             published_at=utcnow() - timedelta(hours=20))
    session.add(p)
    session.flush()
    session.add(PostMetrics(post_id=p.id, ga_sessions=sessions, collected_at=utcnow()))
    session.commit()
    return a


def test_week_top_never_returns_our_own_digests(session):
    _digest_post(session, sessions=9000)
    real = _article(session, "wt-1", "Īsts raksts", sessions=100)
    top = weekend.week_top(session, limit=5)
    assert [a.id for a in top] == [real.id]


def test_wednesday_question_is_about_a_real_article_with_a_picture(session, monkeypatch):
    """Ekrānuzņēmuma gadījums: jautājums «par TOP 5» ar sarkanu kadru un saiti
    uz sākumlapu — tā dzima, jo digest pats bija nedēļas lasītākais."""
    _digest_post(session, sessions=9000)
    bare = _article(session, "q-bare", "Raksts bez attēla", sessions=800, images=[])
    real = _article(session, "q-real", "Lielais notikums Rīgā", sessions=500)
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    monkeypatch.setattr(cards, "render_share_image", lambda *a, **k: "data/cards/q.png")
    monkeypatch.setattr(weekend, "_ai_lines",
                        lambda *a, **k: ["Vai tev šķiet, ka lēmums bija pareizs?"])
    post = weekend.build_question(session, datetime(2026, 9, 2).date())
    assert post is not None
    assert post.article_id == real.id and post.article_id != bare.id
    assert post.link_url == real.canonical_url
    assert "Lielais notikums Rīgā" in post.copy


def test_top5_carousel_carries_its_reading_list_and_links_to_the_top_story(session, monkeypatch):
    arts = [_article(session, f"t5-{i}", f"Stāsts numur {i}", sessions=500 - i)
            for i in range(5)]
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    monkeypatch.setattr(cards, "render_cards",
                        lambda *a, **k: [f"data/cards/c{i}.png" for i in range(5)])
    post = weekend.build_top5(session, datetime(2026, 9, 5).date(), None)
    assert post is not None
    items = post.extra["items"]
    assert [it["title"] for it in items] == [f"Stāsts numur {i}" for i in range(5)]
    assert items[0]["url"] == arts[0].canonical_url
    assert post.link_url == arts[0].canonical_url          # ne sākumlapa
    assert post.article.url == arts[0].canonical_url


def test_reading_list_goes_to_the_caption_as_titles_and_to_the_comment_with_links(session):
    a = _article(session, "rl-1", "Pirmais", sessions=1)
    post = Post(article_id=a.id, channel="fb_tv3lv", format="card_carousel",
                copy="Nedēļas TOP — pieci notikumi.", link_url=a.canonical_url,
                state="scheduled", hook_type="digest",
                extra={"items": [{"article": 1, "title": "Pirmais", "url": "https://tv3.lv/a"},
                                 {"article": 2, "title": "Otrais", "url": "https://tv3.lv/b"}]})
    session.add(post)
    session.commit()
    rules = {"link_in_first_comment": True, "link_in_caption": True}
    text, in_comment = pipeline.compose_text(post, "facebook_page",
                                             "https://tv3.lv/a?utm_content=x", rules)
    assert "1. Pirmais\n2. Otrais" in text
    assert text.count("https://") == 1            # apraksts tīrs: tikai galvenā saite
    assert in_comment
    comment = pipeline.first_comment_text(post, "facebook_page", "https://tv3.lv/a?x", rules)
    assert "1. Pirmais\nhttps://tv3.lv/a?" in comment and "2. Otrais\nhttps://tv3.lv/b?" in comment
    assert "utm_term=digest-1" in comment and "utm_term=digest-2" in comment
    assert f"utm_content={post.id}" in comment
    # X: 280 zīmēs saraksts neietilpst — paliek teksts un galvenā saite
    x_text, _ = pipeline.compose_text(post, "x", "https://tv3.lv/a?utm=x", rules)
    assert "1. Pirmais" not in x_text and "https://tv3.lv/a?utm=x" in x_text
    # parastam ierakstam komentārā paliek tā pati saite kā līdz šim
    plain = Post(article_id=a.id, channel="fb_tv3lv", format="photo", copy="x",
                 link_url=a.canonical_url, state="scheduled")
    assert pipeline.first_comment_text(plain, "facebook_page", "https://tv3.lv/a?u", rules) == "https://tv3.lv/a?u"


def test_publishing_a_digest_posts_the_reading_list_as_the_first_comment(session, monkeypatch):
    calls = {}

    class FakeAdapter:
        def publish(self, *, text, link, images, fmt, **kw):
            calls["text"] = text
            return "fb-1"

        def comment(self, post_id, message):
            calls["comment"] = message
            return "c1"

    monkeypatch.setattr(pipeline, "get_adapter", lambda platform: FakeAdapter())
    a = _article(session, "pub-d", "Pirmais", sessions=1)
    post = Post(article_id=a.id, channel="fb_tv3lv", format="card_carousel", copy="TOP",
                link_url=a.canonical_url, state="scheduled", hook_type="digest",
                media=["data/cards/x.png"], scheduled_at=utcnow() - timedelta(minutes=1),
                extra={"timeless": True, "card_links": ["https://tv3.lv/a"],
                       "items": [{"article": a.id, "title": "Pirmais", "url": "https://tv3.lv/a"},
                                 {"article": 0, "title": "Otrais", "url": "https://tv3.lv/b"}]})
    session.add(post)
    session.commit()
    assert pipeline.publish_due(session) == 1
    assert "1. Pirmais" in calls["text"]
    assert calls["comment"].startswith("1. Pirmais\nhttps://tv3.lv/a?")
    assert "2. Otrais\nhttps://tv3.lv/b?" in calls["comment"]
