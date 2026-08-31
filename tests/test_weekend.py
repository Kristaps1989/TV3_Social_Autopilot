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


def test_mosaic_cover_and_photo_point_frames():
    from app import cards, reels

    doc = cards.build_mosaic_story_html("Nedēļa 30 sekundēs", "sport",
                                        [f"https://cdn/p{i}.jpg" for i in range(5)],
                                        date_txt="29.08.2026")
    assert doc.count('class="cell"') == 6      # 5 foto -> pirmais atkārtojas
    assert "Nedēļa 30 sekundēs" in doc and "29.08.2026" in doc
    frame = reels._point_frame_html("sport", 1, "Punkts",
                                    bg_image="https://cdn/p0.jpg")
    assert "https://cdn/p0.jpg" in frame and "rgba(12,6,16" in frame
    plain = reels._point_frame_html("sport", 1, "Punkts")
    assert "linear-gradient(160deg" in plain and "url(" not in plain


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
        rendered.update(title=title, points=points, date=kwargs.get("date_txt"),
                        point_dates=kwargs.get("point_dates"))
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
    # karuselī punkti ar absolūtiem datumiem savā rindā, čipam šodienas datums
    assert "augustā" in rendered["point_dates"][0]
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


def test_night_run_builds_nothing(session):
    _article(session, "wd-1", "Notikums", sessions=100)
    # 05:00 Rīgā — naktī nekas netiek būvēts nevienā dienā
    assert weekend.run(session, datetime(2026, 8, 27, 2, 0)) == 0


def test_weekday_without_data_builds_nothing(session):
    """Ceturtdienā ir «pirms gada» franšīze, bet gadu vecu arhīva nav —
    formāts klusē, nevis publicē tukšu ierakstu."""
    _article(session, "wd-2", "Notikums", sessions=100)
    assert weekend.run(session, datetime(2026, 8, 27, 10, 0)) == 0


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
    # datums tekstā ir tā raksta ĪSTAIS publicēšanas datums; mēneša nosaukumu
    # rēķinām tāpat kā kods, citādi tests salūst, mainoties kalendāra dienai
    assert "Joprojām aktuāli" in post.copy
    assert weekend.lv_date(old.published_at) in post.copy
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


def test_top5_carousel_is_five_stories_each_with_photo_and_link(session,
                                                                monkeypatch):
    """FB karuselī ir 5 kartītes. Vāks + CTA aizņēma divas no tām, un no
    «TOP 5» reāli palika trīs stāsti — tāpēc tagad piecas kartītes = pieci
    stāsti, katrs ar savu foto un savu saiti."""
    from app import cards

    arts = [_article(session, f"cl-{i}", f"Notikums numur {i}", sessions=100 - i)
            for i in range(5)]
    arts[2].images = ["https://cdn/photopost/grafika.jpg"]   # nav tīra foto
    session.commit()
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    seen = {}

    def fake_cards(title, section, tag, points, image, question, **kwargs):
        seen.update(points=points, kwargs=kwargs)
        return [f"c{i}.png" for i in range(5)]

    monkeypatch.setattr(cards, "render_cards", fake_cards)
    post = weekend.build_top5(session, SAT.date(), "sport")

    assert len(seen["points"]) == 5
    assert seen["kwargs"]["include_cover"] is False
    assert seen["kwargs"]["include_end"] is False
    assert seen["kwargs"]["label"] == "NEDĒĻAS SPORTA TOP 5"
    # katrai kartītei sava raksta bilde; photopost grafika netiek lietota
    imgs = seen["kwargs"]["point_images"]
    assert len(imgs) == 5 and imgs[0] == "https://cdn/cl-0.jpg"
    assert imgs[2] == ""
    # visas piecas kartītes ved uz SAVIEM rakstiem, nevis uz sadaļas lapu
    links = post.extra["card_links"]
    titles = post.extra["card_titles"]
    assert links == [a.canonical_url for a in arts]
    assert titles == [a.title for a in arts]
    assert "https://tv3.lv/sports" not in links
    # ieraksta teksts joprojām ved uz sadaļu un nes ievadu
    assert post.link_url == "https://tv3.lv/sports"


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
                   include_cover=True, include_end=True,
                   cover_images=None, point_images=None):
        edge = frame_seconds if edge_seconds is None else edge_seconds
        captured.update(title=title, points=points, frame_seconds=frame_seconds,
                        edge=edge, cover_images=cover_images,
                        point_images=point_images,
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
    # vāks = rakstu foto mozaīka; punktu kadri = katra raksta foto fonā
    assert len(captured["cover_images"]) == 5
    assert all(u.startswith("https://cdn/") for u in captured["cover_images"])
    assert len(captured["point_images"]) == 5


MON = datetime(2026, 8, 31, 6, 0)   # pirmdiena 09:00 Rīgā


def _dated_article(session, guid, title, published_at, sessions=0,
                   images=None, section="news", sensitivity=None):
    """Raksts + publicēts posts ar FIKSĒTU laiku — pirmdienas loga testiem."""
    a = Article(guid=guid, url=f"https://tv3.lv/{guid}",
                canonical_url=f"https://tv3.lv/{guid}", title=title,
                section=section, ai_score=0.8,
                sensitivity=sensitivity if sensitivity is not None else [],
                images=images if images is not None
                else [f"https://cdn/uploads/{guid}.jpg"],
                published_at=published_at)
    session.add(a)
    session.flush()
    p = Post(article_id=a.id, channel="fb_tv3lv", format="link", copy=title,
             link_url=a.canonical_url, state="published",
             published_at=published_at)
    session.add(p)
    session.flush()
    if sessions:
        session.add(PostMetrics(post_id=p.id, ga_sessions=sessions,
                                collected_at=utcnow()))
    session.commit()
    return a


def test_weekend_start_is_saturday_riga_midnight():
    from zoneinfo import ZoneInfo

    start = weekend.weekend_start(MON)
    local = start.replace(tzinfo=ZoneInfo("UTC")).astimezone(
        ZoneInfo("Europe/Riga"))
    assert local.weekday() == 5                       # sestdiena
    assert (local.day, local.month) == (29, 8)
    assert (local.hour, local.minute) == (0, 0)


def test_monday_builds_weekend_top5_and_story(session, monkeypatch):
    from app import cards

    # nedēļas nogale: sestdiena + svētdiena (UTC laiki loga iekšpusē)
    wk = [_dated_article(session, f"m-{i}", f"Nogales notikums numur {i}",
                         datetime(2026, 8, 29 + i % 2, 12, 0),
                         sessions=100 - i)
          for i in range(4)]
    # piektdienas hits ar lielāko trafiku NEDRĪKST iekļūt nogales TOP
    friday = _dated_article(session, "m-fri", "Piektdienas lielais stāsts",
                            datetime(2026, 8, 28, 12, 0), sessions=9000)
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    rendered = {}

    def fake_cards(title, section, tag, points, image, question, **kwargs):
        rendered.update(title=title, points=points,
                        date=kwargs.get("date_txt"))
        return ["c0.png", "c1.png", "c2.png", "c3.png", "c4.png", "end.png"]

    monkeypatch.setattr(cards, "render_cards", fake_cards)
    mosaic = {}

    def fake_mosaic(title, section, images, **kwargs):
        mosaic.update(title=title, images=images,
                      date=kwargs.get("date_txt"))
        return "data/cards/mosaic.png"

    monkeypatch.setattr(cards, "render_mosaic_story", fake_mosaic)

    assert weekend.run(session, MON) == 2
    posts = {p.hook_type: p for p in session.execute(select(Post).where(
        Post.hook_type.in_(("mondaytop5", "mondaystory")))).scalars().all()}
    assert set(posts) == {"mondaytop5", "mondaystory"}

    top5 = posts["mondaytop5"]
    assert top5.channel == "fb_tv3lv" and top5.format == "card_carousel"
    assert "nedēļas nogalē" in top5.copy
    # loga datumi tekstā absolūti; nekādu relatīvo vārdu
    assert "29. augustā" in top5.copy and "30. augustā" in top5.copy
    assert weekend.has_relative_words(top5.copy) == ""
    # katra kartīte ved uz savu nogales rakstu; piektdienas hits ārpusē
    links = top5.extra["card_links"]
    assert links[0] == wk[0].canonical_url
    assert friday.canonical_url not in links
    assert "https://tv3.lv" not in links
    # 08:00 Rīgā = 05:00 UTC (vasaras laiks)
    assert top5.scheduled_at == datetime(2026, 8, 31, 5, 0)
    assert rendered["date"] == "31.08.2026"

    story = posts["mondaystory"]
    assert story.channel == "fb_stories" and story.format == "story"
    assert story.media == ["data/cards/mosaic.png"]
    assert story.scheduled_at == datetime(2026, 8, 31, 5, 30)
    assert mosaic["title"] == "Nedēļas nogales TOP 5"
    assert len(mosaic["images"]) >= 3
    assert all("photopost" not in u for u in mosaic["images"])

    # atkārtota izpilde tajā pašā pirmdienā neko nedublē
    assert weekend.run(session, MON) == 0


def test_monday_toggle_off_skips_both(session, monkeypatch):
    from app import cards

    for i in range(4):
        _dated_article(session, f"mt-{i}", f"Nogales notikums numur {i}",
                       datetime(2026, 8, 29, 12, 0), sessions=50)
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    monkeypatch.setattr(cards, "render_cards", lambda *a, **k: ["x.png", "y.png"])
    monkeypatch.setattr(cards, "render_mosaic_story",
                        lambda *a, **k: "data/cards/mosaic.png")
    weekend.save_settings(session, {"top5": True, "reel": True, "icymi": True,
                                    "quiz": True, "evergreen": True,
                                    "monday": False})
    assert weekend.run(session, MON) == 0


def test_monday_story_needs_three_clean_images(session, monkeypatch):
    from app import cards

    # photopost grafikas neskaitās — bez 3 tīriem foto stāsta nav
    for i in range(4):
        _dated_article(session, f"ms-{i}", f"Nogales notikums numur {i}",
                       datetime(2026, 8, 29, 12, 0), sessions=50,
                       images=[f"https://cdn/photopost/g{i}.jpg"])
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    assert weekend.build_monday_story(session, MON.date(), MON) is None


def test_photopost_graphics_never_reach_digest_visuals(session, monkeypatch):
    """Photopost grafikām ir savs iestrādāts virsraksts — mozaīkā un zem
    mūsu teksta tas dublētos, tāpēc digest ņem tikai tīros foto."""
    from app import cards, reels

    a1 = _article(session, "pp-1", "Raksts ar photopost un tīro foto",
                  sessions=90)
    a1.images = ["https://cdn/photopost/grafika.jpg",
                 "https://cdn/uploads/tirs-foto1.jpg"]
    a2 = _article(session, "pp-2", "Raksts tikai ar photopost", sessions=80)
    a2.images = ["https://cdn/photopost/grafika2.jpg"]
    a3 = _article(session, "pp-3", "Raksts ar parastu foto", sessions=70)
    a3.images = ["https://cdn/uploads/tirs-foto3.jpg"]
    session.commit()

    assert weekend._clean_image(a1) == "https://cdn/uploads/tirs-foto1.jpg"
    assert weekend._clean_image(a2) == ""      # nav tīra foto -> bez attēla
    assert weekend._clean_image(a3) == "https://cdn/uploads/tirs-foto3.jpg"

    monkeypatch.setattr(reels, "available", lambda: True)
    captured = {}

    def fake_build(title, section, image, points, cover_images=None,
                   point_images=None, **kwargs):
        captured.update(cover_images=cover_images, point_images=point_images)
        return "data/cards/digest.mp4"

    monkeypatch.setattr(reels, "build_reel", fake_build)
    weekend.build_reel_digest(session, SAT.date())
    assert all("photopost" not in u for u in captured["cover_images"])
    # a2 punkta kadrs paliek bez fona (gradienta variants), ne ar grafiku
    assert "" in captured["point_images"]
    assert all("photopost" not in u for u in captured["point_images"] if u)

    # karuseļa vāks (plāksne pa virsu) arī ņem tikai tīro foto
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    rendered = {}

    def fake_cards(title, section, tag, points, image, question, **kwargs):
        rendered["image"] = image
        return ["c0.png", "c1.png"]

    monkeypatch.setattr(cards, "render_cards", fake_cards)
    weekend.build_top5(session, SAT.date(), "sport")
    assert "photopost" not in rendered["image"]


TUE = datetime(2026, 9, 1, 9, 0)    # otrdiena 12:00 Rīgā
WED = datetime(2026, 9, 2, 17, 0)   # trešdiena 20:00 Rīgā
THU = datetime(2026, 9, 3, 12, 0)   # ceturtdiena 15:00 Rīgā
FRI = datetime(2026, 9, 4, 14, 0)   # piektdiena 17:00 Rīgā


def _only(session, feature):
    weekend.save_settings(session, {feature: True})


def test_daily_story_uses_todays_window_only(session, monkeypatch):
    from app import cards

    for i in range(3):
        _dated_article(session, f"ds-{i}", f"Trešdienas notikums numur {i}",
                       datetime(2026, 9, 2, 9, 0), sessions=100 - i)
    _dated_article(session, "ds-old", "Otrdienas hits",
                   datetime(2026, 9, 1, 9, 0), sessions=9000)
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    seen = {}

    def fake_mosaic(title, section, images, **kwargs):
        seen.update(title=title, images=images, date=kwargs.get("date_txt"))
        return "data/cards/day.png"

    monkeypatch.setattr(cards, "render_mosaic_story", fake_mosaic)
    _only(session, "daily_story")

    assert weekend.run(session, WED) == 1
    post = session.execute(select(Post).where(
        Post.hook_type == "dailystory")).scalars().one()
    assert post.channel == "fb_stories" and post.format == "story"
    assert post.media == ["data/cards/day.png"]
    assert post.scheduled_at == datetime(2026, 9, 2, 17, 0)   # 20:00 Rīgā
    assert seen["title"] == "Dienas TOP 3" and seen["date"] == "02.09.2026"
    # vakardienas foto dienas mozaīkā neiekļūst
    assert all("ds-old" not in u for u in seen["images"])


def test_evening_formats_are_not_built_in_the_morning(session, monkeypatch):
    from app import cards

    for i in range(3):
        _dated_article(session, f"dm-{i}", f"Rīta notikums numur {i}",
                       datetime(2026, 9, 2, 5, 0), sessions=50)
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    monkeypatch.setattr(cards, "render_mosaic_story",
                        lambda *a, **k: "data/cards/day.png")
    _only(session, "daily_story")
    # 09:00 Rīgā: dienas TOP no rīta datiem būtu meli — formāts gaida vakaru
    assert weekend.run(session, datetime(2026, 9, 2, 6, 0)) == 0
    assert weekend.run(session, WED) == 1


def test_friday_guide_takes_entertainment_only(session, monkeypatch):
    from app import cards

    ents = [_dated_article(session, f"g-{i}", f"Izklaides notikums numur {i}",
                           datetime(2026, 9, 1 + i, 9, 0), sessions=100 - i,
                           section="entertainment") for i in range(3)]
    _dated_article(session, "g-news", "Ziņu hits", datetime(2026, 9, 2, 9, 0),
                   sessions=9000)
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    seen = {}

    def fake_cards(title, section, tag, points, image, question, **kwargs):
        seen.update(title=title, section=section, tag=tag, points=points)
        return ["c0.png", "c1.png", "c2.png", "end.png"]

    monkeypatch.setattr(cards, "render_cards", fake_cards)
    _only(session, "guide")

    assert weekend.run(session, FRI) == 1
    post = session.execute(select(Post).where(
        Post.hook_type == "guide")).scalars().one()
    assert post.format == "card_carousel"
    assert post.link_url == "https://tv3.lv/izklaide"
    assert post.scheduled_at == datetime(2026, 9, 4, 14, 0)   # 17:00 Rīgā
    assert seen["section"] == "entertainment" and seen["tag"] == "#BRĪVDIENĀM"
    # ziņu hits gidā neiekļūst, arī ja tam ir vairāk sesiju
    assert all("Ziņu hits" not in pt for pt in seen["points"])
    assert post.extra["card_links"][0] == ents[0].canonical_url


def test_wednesday_question_is_a_photo_post_linking_to_the_article(
        session, monkeypatch):
    from app import cards

    art = _dated_article(session, "qq-1", "Lielais notikums Rīgā",
                         datetime(2026, 9, 1, 9, 0), sessions=500)
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    seen = {}

    def fake_share(title, section, image, kicker="", **kwargs):
        seen.update(title=title, kicker=kicker, height=kwargs.get("height"))
        return "data/cards/q.png"

    monkeypatch.setattr(cards, "render_share_image", fake_share)
    monkeypatch.setattr(weekend, "_ai_lines",
                        lambda *a, **k: ["Vai tev šķiet, ka lēmums bija pareizs?"])
    _only(session, "question")

    assert weekend.run(session, datetime(2026, 9, 2, 16, 0)) == 1
    post = session.execute(select(Post).where(
        Post.hook_type == "question")).scalars().one()
    # foto ieraksts -> saite gan tekstā, gan pirmajā komentārā (pipeline)
    assert post.format == "photo" and post.article_id == art.id
    assert post.link_url == art.canonical_url
    assert post.scheduled_at == datetime(2026, 9, 2, 16, 0)   # 19:00 Rīgā
    assert "Vai tev šķiet" in post.copy and "komentāros" in post.copy
    assert seen["kicker"] == "JAUTĀJUMS" and seen["height"] == 1350


def test_question_rejects_weak_ai_output(session, monkeypatch):
    from app import cards

    _dated_article(session, "qq-2", "Notikums", datetime(2026, 9, 1, 9, 0),
                   sessions=500)
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    monkeypatch.setattr(cards, "render_share_image",
                        lambda *a, **k: "data/cards/q.png")
    # apgalvojums bez jautājuma zīmes
    monkeypatch.setattr(weekend, "_ai_lines", lambda *a, **k: ["Interesants stāsts."])
    assert weekend.build_question(session, WED.date()) is None
    # jautājums ar relatīvu laika vārdu — novecojot melotu
    monkeypatch.setattr(weekend, "_ai_lines",
                        lambda *a, **k: ["Vai vakar redzēji, kas notika?"])
    assert weekend.build_question(session, WED.date()) is None
    # bez AI atslēgas (tukšs saraksts) formāts vienkārši izlaižas
    monkeypatch.setattr(weekend, "_ai_lines", lambda *a, **k: [])
    assert weekend.build_question(session, WED.date()) is None


def test_year_ago_picks_the_anniversary_and_skips_sensitive(session, monkeypatch):
    hit = _dated_article(session, "ya-1", "Pirms gada atklāja jauno tiltu",
                         datetime(2025, 9, 3, 10, 0), sessions=400)
    # jutīga tēma nekad neatgriežas kā nostalģija
    _dated_article(session, "ya-bad", "Traģēdija uz ceļa",
                   datetime(2025, 9, 3, 11, 0), sessions=9000,
                   sensitivity=["tragedy"])
    # pusgadu vecs raksts nav gadadiena
    _dated_article(session, "ya-mid", "Pavasara stāsts",
                   datetime(2026, 3, 3, 10, 0), sessions=8000)
    _only(session, "yearago")

    assert weekend.run(session, THU) == 1
    post = session.execute(select(Post).where(
        Post.hook_type == "yearago")).scalars().one()
    assert post.article_id == hit.id and post.format == "link"
    assert post.scheduled_at == datetime(2026, 9, 3, 12, 0)   # 15:00 Rīgā
    assert "Šajā dienā pirms gada" in post.copy
    assert "2025. gada 3. septembrī" in post.copy
    assert weekend.has_relative_words(post.copy) == ""


def test_number_card_publishes_only_with_a_real_number(session, monkeypatch):
    from app import cards

    art = _dated_article(session, "nb-1", "Budžetā trūkst 47 miljoni eiro",
                         datetime(2026, 9, 1, 6, 0), sessions=700)
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    seen = {}

    def fake_number(number, context, section, image="", **kwargs):
        seen.update(number=number, context=context, section=section)
        return "data/cards/n.png"

    monkeypatch.setattr(cards, "render_number_card", fake_number)
    # AI neatrod pārliecinošu skaitli -> diena paliek tukša
    monkeypatch.setattr(weekend, "_ai_lines", lambda *a, **k: ["NAV"])
    assert weekend.build_number(session, TUE.date()) is None

    monkeypatch.setattr(weekend, "_ai_lines", lambda *a, **k: [
        "47 milj. €", "Tik daudz nākamgad trūkst pašvaldību budžetos"])
    _only(session, "number")
    assert weekend.run(session, TUE) == 1
    post = session.execute(select(Post).where(
        Post.hook_type == "number")).scalars().one()
    assert post.format == "photo" and post.article_id == art.id
    assert post.link_url == art.canonical_url
    assert post.scheduled_at == datetime(2026, 9, 1, 9, 0)    # 12:00 Rīgā
    assert seen["number"] == "47 milj. €"
    assert post.copy.startswith("47 milj. €")


def test_quiz_moved_to_the_sunday_evening_peak(session, monkeypatch):
    from app import cards

    for i in range(3):
        _article(session, f"qz-{i}", f"Notikums numur {i}", sessions=50 + i)
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    monkeypatch.setattr(cards, "render_cards", lambda *a, **k: ["q0.png", "q1.png"])
    monkeypatch.setattr(weekend, "_ai_lines", lambda *a, **k: [
        "Kurš uzvarēja 29. augustā?", "Cik punktus guva komanda?",
        "Kurā pilsētā notika spēle?"])
    post = weekend.build_quiz(session, SUN.date())
    assert post is not None
    assert post.scheduled_at == datetime(2026, 8, 30, 16, 0)  # 19:00 Rīgā


def test_number_card_layout_scales_and_carries_the_date():
    from app import cards

    doc = cards.build_number_html("47 milj. €", "Tik daudz trūkst budžetos",
                                  "news", "https://cdn/x.jpg", "01.09.2026")
    assert "NEDĒĻAS SKAITLIS" in doc and "47 milj. €" in doc
    assert "01.09.2026" in doc and "https://cdn/x.jpg" in doc
    # garš skaitlis nedrīkst izplūst ārpus kartes -> mazāks fonts
    assert "font-size:300px" in cards.build_number_html("47%", "K", "news")
    assert "font-size:120px" in doc      # 10 rakstzīmes -> mazākais fonts


def test_quiz_cards_link_to_the_article_that_holds_the_answer(session,
                                                              monkeypatch):
    from app import cards

    arts = [_article(session, f"ql-{i}", f"Notikums numur {i}", sessions=100 - i)
            for i in range(3)]
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    monkeypatch.setattr(cards, "render_cards",
                        lambda *a, **k: ["cover.png", "q1.png", "q2.png",
                                         "q3.png", "end.png"])
    # AI atbild «numurs | jautājums» — numurs norāda rakstu ar atbildi
    monkeypatch.setattr(weekend, "_ai_lines", lambda *a, **k: [
        "2 | Kurš uzvarēja 26. augustā?",
        "1 | Cik punktus guva komanda?",
        "9 | Kurā pilsētā notika spēle?"])          # ārpus saraksta
    post = weekend.build_quiz(session, SUN.date())
    assert post is not None

    links = post.extra["card_links"]
    assert links[0] == "https://tv3.lv"                  # vāks -> portāls
    assert links[1] == arts[1].canonical_url             # «2 |» -> otrais
    assert links[2] == arts[0].canonical_url             # «1 |» -> pirmais
    assert links[3] == "https://tv3.lv"                  # nezināms -> portāls
    assert links[4] == "https://tv3.lv"                  # CTA -> portāls
    # virsrakstu josla nenodod atbildi: neitrāls aicinājums, ne raksta virsraksts
    titles = post.extra["card_titles"]
    assert titles[1] == "Atbilde — tv3.lv"
    assert all(a.title not in titles for a in arts)


def test_every_carousel_card_gets_its_own_utm_term(session, monkeypatch):
    """Bez kartīšu saitēm visas kartītes dalījās vienā UTM — nevarēja pateikt,
    kura kartīte nopelnīja klikšķi."""
    import app.pipeline as pl

    captured = {}

    class FakeAdapter:
        def publish(self, *, text, link, images, fmt, card_links=None,
                    card_titles=None):
            captured.update(card_links=card_links)
            return "fb-1"

        def comment(self, post_id, message):
            return "c1"

    monkeypatch.setattr(pl, "get_adapter", lambda platform: FakeAdapter())
    a = _article(session, "utm-1", "Viena raksta karuselis")
    p = Post(article_id=a.id, channel="fb_tv3lv", format="card_carousel",
             copy="Teksts", link_url=a.canonical_url, hook_type="quiz",
             media=["c0.png", "c1.png", "c2.png"],
             state="scheduled", scheduled_at=utcnow() - timedelta(minutes=1))
    session.add(p)
    session.commit()
    assert pl.publish_due(session) == 1

    links = captured["card_links"]
    assert len(links) == 3
    # katra kartīte ved uz to pašu rakstu, bet ar savu numuru UN franšīzi
    assert all(a.canonical_url in u for u in links)
    assert "utm_term=quiz-karte1" in links[0]
    assert "utm_term=quiz-karte3" in links[2]
