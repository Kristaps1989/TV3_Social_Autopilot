from datetime import datetime, timedelta

from sqlalchemy import select

from app import weekend
from app.models import Article, Post, PostMetrics, utcnow

SAT = datetime(2026, 8, 29, 8, 0)   # sestdiena 11:00 Rīgā
SUN = datetime(2026, 8, 30, 8, 0)


def _article(session, guid, title, section="sport", sessions=0, score=0.8,
             age_days=2):
    a = Article(guid=guid, url=f"https://tv3.lv/{guid}",
                canonical_url=f"https://tv3.lv/{guid}", title=title,
                section=section, ai_score=score,
                images=[f"https://cdn/{guid}.jpg"],
                published_at=utcnow() - timedelta(days=age_days))
    session.add(a)
    session.flush()
    p = Post(article_id=a.id, channel="fb_tv3lv", format="link", copy=title,
             link_url=a.canonical_url, state="published",
             published_at=utcnow() - timedelta(days=min(age_days, 6)))
    session.add(p)
    session.flush()
    if sessions:
        session.add(PostMetrics(post_id=p.id, ga_sessions=sessions,
                                collected_at=utcnow()))
    session.commit()
    return a


def test_lv_date_and_relative_words():
    assert weekend.lv_date(datetime(2026, 8, 26)) == "26. augustā"
    assert "2025. gads" in weekend.lv_date(datetime(2025, 12, 3))
    assert weekend.has_relative_words("Vakar notika spēle") == "vakar"
    assert weekend.has_relative_words("26. augustā notika spēle") == ""


def test_week_top_prefers_measured_sessions(session):
    _article(session, "w-1", "Mazlasīts raksts", sessions=10)
    top = _article(session, "w-2", "Lielais notikums", sessions=900)
    _article(session, "w-3", "Vidējais raksts", sessions=100)
    out = weekend.week_top(session, section="sport", limit=2)
    assert out[0].id == top.id and len(out) == 2


def test_saturday_builds_sport_top5_and_reel(session, monkeypatch):
    from app import cards, reels

    for i in range(4):
        _article(session, f"s-{i}", f"Sporta notikums numur {i}", sessions=50 + i)
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    rendered = {}

    def fake_cards(title, section, tag, points, image, question, **kwargs):
        rendered.update(title=title, points=points, date=kwargs.get("date_txt"))
        return ["data/cards/d1.png", "data/cards/d2.png"]

    monkeypatch.setattr(cards, "render_cards", fake_cards)
    monkeypatch.setattr(reels, "available", lambda: True)
    monkeypatch.setattr(reels, "build_reel",
                        lambda *a, **k: "data/cards/digest.mp4")

    created = weekend.run(session, SAT)
    assert created == 3  # top5 + reels + icymi
    posts = session.execute(select(Post).where(
        Post.hook_type.in_(("digest", "digestreel", "icymi")))).scalars().all()
    kinds = {p.hook_type: p for p in posts}
    assert set(kinds) == {"digest", "digestreel", "icymi"}
    # karuselī punkti ar absolūtiem datumiem, vāka čipam šodienas datums
    assert "augustā" in rendered["points"][0]
    assert rendered["date"] == "29.08.2026"
    # icymi teksts satur publicēšanas datumu un nesatur relatīvus vārdus
    icymi = kinds["icymi"]
    assert "Publicēts" in icymi.copy and "augustā" in icymi.copy
    assert weekend.has_relative_words(icymi.copy) == ""
    # otrā izpilde tajā pašā dienā neko nedublē
    assert weekend.run(session, SAT) == 0


def test_disabled_features_are_skipped(session, monkeypatch):
    from app import cards, reels

    for i in range(4):
        _article(session, f"d-{i}", f"Notikums numur {i}", sessions=50)
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    monkeypatch.setattr(cards, "render_cards",
                        lambda *a, **k: ["data/cards/x.png"])
    monkeypatch.setattr(reels, "available", lambda: True)
    monkeypatch.setattr(reels, "build_reel", lambda *a, **k: "data/cards/x.mp4")
    weekend.save_settings(session, {"top5": True, "reel": False,
                                    "icymi": False, "quiz": False,
                                    "evergreen": False})
    created = weekend.run(session, SAT)
    assert created == 1
    posts = session.execute(select(Post).where(
        Post.hook_type != "")).scalars().all()
    assert [p.hook_type for p in posts] == ["digest"]


def test_weekday_run_is_inert(session):
    _article(session, "wd-1", "Notikums", sessions=100)
    assert weekend.run(session, datetime(2026, 8, 27, 10, 0)) == 0  # ceturtdiena


def test_evergreen_picks_old_still_read_article(session, monkeypatch):
    old = _article(session, "ev-1", "Mūžzaļais tests par miegu",
                   section="entertainment", sessions=300, age_days=30)
    _article(session, "ev-2", "Svaiga ziņa", sessions=500, age_days=1)
    weekend.save_settings(session, {"top5": False, "reel": False,
                                    "icymi": False, "quiz": False,
                                    "evergreen": True})
    created = weekend.run(session, SUN)
    assert created == 1
    post = session.execute(select(Post).where(
        Post.hook_type == "evergreen")).scalars().one()
    assert post.article_id == old.id
    assert "Joprojām aktuāli" in post.copy and "jūlijā" in post.copy
    # tas pats raksts otro reizi netiek atkārtots
    for key in list(weekend.FEATURES):
        pass
    from app.models import set_setting

    set_setting(session, "weekend:ran:evergreen:2026-09-06", "")
    assert weekend.build_evergreen(session, datetime(2026, 9, 6).date()) is None


def test_quiz_skipped_without_ai_key(session, monkeypatch):
    from app import credentials

    for i in range(4):
        _article(session, f"q-{i}", f"Notikums numur {i}", sessions=50)
    monkeypatch.setattr(credentials, "get", lambda key, session=None: "")
    assert weekend.build_quiz(session, SUN.date()) is None
