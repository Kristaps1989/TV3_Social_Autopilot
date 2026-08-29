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
             published_at=utcnow() - timedelta(hours=2))
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


def test_week_starts_on_monday_riga_time(session):
    from zoneinfo import ZoneInfo

    # piektdiena, 2026-09-04 12:00 UTC -> pirmdiena ir 31. augusts Rīgā
    start = weekend.week_start(datetime(2026, 9, 4, 12, 0))
    local = start.replace(tzinfo=ZoneInfo("UTC")).astimezone(
        ZoneInfo("Europe/Riga"))
    assert local.weekday() == 0 and (local.hour, local.minute) == (0, 0)
    assert local.day == 31 and local.month == 8

    # iepriekšējās nedēļas svētdienas ieraksts NAV šīs nedēļas TOP
    a_old = Article(guid="wk-old", url="u1", canonical_url="u1",
                    title="Pagājušās svētdienas hits", section="sport")
    a_new = Article(guid="wk-new", url="u2", canonical_url="u2",
                    title="Otrdienas raksts", section="sport")
    session.add_all([a_old, a_new])
    session.flush()
    p_old = Post(article_id=a_old.id, channel="fb_tv3lv", format="link",
                 state="published", published_at=datetime(2026, 8, 30, 12, 0))
    p_new = Post(article_id=a_new.id, channel="fb_tv3lv", format="link",
                 state="published", published_at=datetime(2026, 9, 1, 12, 0))
    session.add_all([p_old, p_new])
    session.flush()
    session.add(PostMetrics(post_id=p_old.id, ga_sessions=999,
                            collected_at=utcnow()))
    session.add(PostMetrics(post_id=p_new.id, ga_sessions=10,
                            collected_at=utcnow()))
    session.commit()
    top = weekend.week_top(session, section="sport",
                           now=datetime(2026, 9, 4, 12, 0))
    assert [a.id for a in top] == [a_new.id]


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


def test_top5_stores_per_card_links(session, monkeypatch):
    from app import cards

    arts = [_article(session, f"cl-{i}", f"Notikums numur {i}", sessions=100 - i)
            for i in range(4)]
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    monkeypatch.setattr(cards, "render_cards", lambda *a, **k: [
        "c0.png", "c1.png", "c2.png", "c3.png", "c4.png", "end.png"])
    post = weekend.build_top5(session, SAT.date(), "sport")
    links = post.extra["card_links"]
    assert len(links) == 6
    assert links[0] == "https://tv3.lv/sports" and links[-1] == "https://tv3.lv/sports"
    # punktu kartītes ved uz SAVIEM rakstiem TOP secībā
    assert links[1] == arts[0].canonical_url
    assert links[4] == arts[3].canonical_url
    # katrai kartītei savs virsraksts FB teksta joslai zem attēla
    titles = post.extra["card_titles"]
    assert len(titles) == 6
    assert titles[1] == arts[0].title and titles[-1] == "Visi stāsti — tv3.lv"


def test_publish_passes_card_links_with_per_card_utm(session, monkeypatch):
    import app.pipeline as pl

    captured = {}

    class FakeAdapter:
        def publish(self, *, text, link, images, fmt, card_links=None,
                    card_titles=None):
            captured.update(link=link, card_links=card_links,
                            card_titles=card_titles)
            return "fb-9"

        def comment(self, post_id, message):
            return "c1"

    monkeypatch.setattr(pl, "get_adapter", lambda platform: FakeAdapter())
    a = _article(session, "cl-pub", "Digest tests")
    p = Post(article_id=a.id, channel="fb_tv3lv", format="card_carousel",
             copy="Teksts", link_url="https://tv3.lv/sports",
             media=["c0.png", "c1.png", "end.png"],
             extra={"card_links": ["https://tv3.lv/sports",
                                   "https://tv3.lv/raksts-viens", ""]},
             state="scheduled", scheduled_at=utcnow() - timedelta(minutes=1))
    session.add(p)
    session.commit()
    assert pl.publish_due(session) == 1
    links = captured["card_links"]
    assert "utm_content" in links[0] and "utm_term=karte1" in links[0]
    assert links[1].startswith("https://tv3.lv/raksts-viens?")
    assert "utm_term=karte2" in links[1]
    assert links[2] == ""   # tukša saite paliek tukša -> adapteris liek galveno
    # bez explicit virsrakstiem: raksta virsraksts, pēdējai (CTA) kartītei aicinājums
    assert captured["card_titles"] == ["Digest tests", "Digest tests",
                                       "Lasi visu rakstā — tv3.lv"]


def test_fb_carousel_uses_per_card_links_and_trim_keeps_last(monkeypatch, ):
    import json as _json

    import httpx

    from adapters import facebook

    monkeypatch.setattr(facebook.credentials, "get",
                        lambda key, session=None: {"fb_page_id": "520",
                                                   "fb_page_token": "tok"}.get(key, ""))
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.example")
    calls = []

    class R:
        status_code = 200
        text = ""

        def json(self):
            return {"id": "520_77"}

    monkeypatch.setattr(httpx, "post",
                        lambda url, data=None, files=None, timeout=None:
                        (calls.append((url, dict(data or {}))), R())[1])
    imgs = [f"/d/c{i}.png" for i in range(5)] + ["/d/end.png"]
    links = [f"https://tv3.lv/r{i}" for i in range(5)] + ["https://tv3.lv"]
    titles = [f"Virsraksts {i}" for i in range(5)] + ["Visi stāsti"]
    out = facebook.FacebookPageAdapter().publish(
        text="T", link="https://tv3.lv/sports", images=imgs,
        fmt="card_carousel", card_links=links, card_titles=titles)
    assert out == "520_77"
    cards_sent = _json.loads(calls[0][1]["child_attachments"])
    # 6 -> 5 kartītes: pirmās četras + CTA; saites seko līdzi tam pašam griezumam
    assert [c["link"] for c in cards_sent] == \
        ["https://tv3.lv/r0", "https://tv3.lv/r1", "https://tv3.lv/r2",
         "https://tv3.lv/r3", "https://tv3.lv"]
    # name aizpilda FB kartītes teksta joslu; griezums saskan arī virsrakstiem
    assert [c["name"] for c in cards_sent] == \
        ["Virsraksts 0", "Virsraksts 1", "Virsraksts 2", "Virsraksts 3",
         "Visi stāsti"]


def test_weekly_ai_report_skips_without_key_and_sends_when_ok(session, monkeypatch):
    from app import credentials, overview
    import app.pipeline as pl

    sent = []
    monkeypatch.setattr(pl, "alert", lambda msg: sent.append(msg))
    monkeypatch.setattr(credentials, "get", lambda key, session=None: "")
    overview.weekly_ai_report(session)
    assert sent == []

    monkeypatch.setattr(credentials, "get",
                        lambda key, session=None: "sk-x" if key == "anthropic_api_key" else "")
    monkeypatch.setattr(overview, "ai_report",
                        lambda s: "1. Pārcel budžetu uz Meta.")
    overview.weekly_ai_report(session)
    assert len(sent) == 1 and "pirmdienas apskats" in sent[0]
    assert "Pārcel budžetu" in sent[0]


def test_reel_digest_is_exactly_thirty_seconds(session, monkeypatch):
    from app import reels

    for i in range(5):
        _article(session, f"rd-{i}", f"Garš virsraksts par notikumu numur {i}",
                 sessions=100 - i)
    monkeypatch.setattr(reels, "available", lambda: True)
    captured = {}

    def fake_build(title, section, image, points, out_dir=None, max_points=3,
                   frame_seconds=2.8, edge_seconds=None,
                   include_cover=True, include_end=True):
        edge = frame_seconds if edge_seconds is None else edge_seconds
        captured.update(title=title, points=points, frame_seconds=frame_seconds,
                        edge=edge,
                        total=len(points[:max_points]) * frame_seconds
                              + (edge if include_cover else 0)
                              + (edge if include_end else 0))
        return "data/cards/digest.mp4"

    monkeypatch.setattr(reels, "build_reel", fake_build)
    post = weekend.build_reel_digest(session, SAT.date())
    assert post is not None and "30 sekundēs" in post.copy
    assert captured["title"] == "Nedēļa 30 sekundēs"
    # saturs: 5 punkti × 6 s = 30 s; intro + outro pa 3 s -> kopā 36 s
    assert captured["frame_seconds"] == 6.0 and captured["edge"] == 3.0
    assert len(captured["points"]) == 5
    assert captured["total"] == 36.0
