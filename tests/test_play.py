"""TV3 Play promo: katalogs no sitemapiem, ētikas sargi, kvotas, logs, UTM."""
from datetime import datetime, timedelta

from app import config, play, slots
from app.models import Article, Post, set_setting, utcnow
from app.rules_engine import Verdict, evaluate

SITEMAP = """<?xml version="1.0"?><urlset xmlns:video="http://www.google.com/schemas/sitemap-video/1.1">
<url><loc>https://play.tv3.lv/filmas/tosts-par-milestibu-6205267/</loc><lastmod>2026-08-30T20:00:00+03:00</lastmod>
<video:video><video:thumbnail_loc>https://tv3cdn.lv/t/tosts.jpg</video:thumbnail_loc><video:title>Tosts par mīlestību</video:title>
<video:player_loc>https://play.tv3.lv/goTo/6205267</video:player_loc><video:publication_date>2026-08-30T19:00:00+03:00</video:publication_date>
<video:duration>5580</video:duration><video:description>Romantiska komēdija par kāzām.</video:description></video:video></url>
<url><loc>https://play.tv3.lv/video/klarksona-ferma-7426847/serija-3-7426850/</loc><lastmod>2026-08-29T20:00:00+03:00</lastmod>
<video:video><video:thumbnail_loc>https://tv3cdn.lv/t/kf.jpg</video:thumbnail_loc><video:title>3. sērija</video:title>
<video:player_loc>https://play.tv3.lv/goTo/7426850</video:player_loc><video:publication_date>2026-08-29T19:00:00+03:00</video:publication_date>
<video:duration>2700</video:duration><video:description>Džeremijs sēj.</video:description></video:video></url>
<url><loc>https://play.tv3.lv/video/tv3-zinas-2530780/31augusts-12292095/</loc>
<video:video><video:title>31.augusts</video:title><video:duration>2473</video:duration></video:video></url>
<url><loc>https://play.tv3.lv/video/900-sekundes-2598929/ko-brivdienas-12084795/</loc>
<video:video><video:title>Ko brīvdienās</video:title><video:duration>180</video:duration></video:video></url>
<url><loc>https://play.tv3.lv/tiesraides/tv3-lv-2831095/bez-tabu-12272731/</loc></url>
</urlset>"""

SHOW_PAGE = """<html><head><meta property="og:title" content="Klārksona ferma | TV3 Play">
<meta property="og:image" content="https://tv3cdn.lv/p/kf-poster.jpg"><meta property="video:tag" content="Dokumentāls">
<meta property="video:tag" content="Humors"></head><body></body></html>"""
MOVIE_PAGE = """<html><head><meta property="og:title" content="Tosts par mīlestību | TV3 Play">
<script type="application/ld+json">{"@type":"Movie","name":"Tosts par mīlestību","genre":["Romantiskā komēdija"],"contentRating":"12+"}</script>
</head><body></body></html>"""
NOW = datetime(2026, 9, 3, 17, 0)   # trešdiena 20:00 Rīgā


def _fetch(url, timeout=10):
    return {"https://play.tv3.lv/sitemaps/sitemap-latest.xml": SITEMAP,
            "https://play.tv3.lv/sitemaps/sitemap-2026-09.xml": "",
            "https://play.tv3.lv/video/klarksona-ferma-7426847/": SHOW_PAGE,
            "https://play.tv3.lv/filmas/tosts-par-milestibu-6205267/": MOVIE_PAGE}.get(url, "")


def _enabled(monkeypatch, **extra):
    base = dict(config.load_rules())
    monkeypatch.setattr(config, "load_rules", lambda: {**base, "play": {"enabled": True, **extra}})


def _news(session, guid, title, sensitivity=None, at=None, channel="fb_play"):
    a = Article(guid=guid, url=f"https://tv3.lv/{guid}/", canonical_url=f"https://tv3.lv/{guid}/",
                title=title, section="news", sensitivity=sensitivity or [], published_at=NOW)
    session.add(a)
    session.flush()
    p = Post(article_id=a.id, channel=channel, format="link", copy=title,
             scheduled_at=at or NOW, state="scheduled", extra={})
    session.add(p)
    session.flush()
    return a, p


def test_catalog_parses_sitemaps_and_excludes_news_programmes_and_clips(monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    cfg = play.settings({})
    assert play.sitemap_urls(cfg, NOW) == ["https://play.tv3.lv/sitemaps/sitemap-latest.xml",
                                           "https://play.tv3.lv/sitemaps/sitemap-2026-09.xml"]
    items = play.catalog(_fetch, cfg, NOW)
    kinds = {i["id"]: i for i in items}
    assert kinds["6205267"]["kind"] == "movie" and kinds["6205267"]["seconds"] == 5580
    assert kinds["7426850"]["kind"] == "episode" and kinds["7426850"]["show"] == "klarksona-ferma"
    assert kinds["7426850"]["show_id"] == "7426847"
    assert "tiesraides" not in " ".join(i["kind"] for i in items)
    assert play.excluded(kinds["12292095"], cfg).startswith("ziņu raidījums")
    assert play.excluded(kinds["12084795"], cfg).startswith("ziņu raidījums")
    assert play.excluded({"kind": "episode", "show": "kaut-kas", "seconds": 120}, cfg).startswith("par īsu")
    assert play.excluded(kinds["6205267"], cfg) == ""


def test_crawl_is_off_by_default_and_builds_rows_with_genres_when_on(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    assert play.crawl(session, fetch=_fetch, now=NOW) == {**play.crawl(session, fetch=_fetch, now=NOW),
                                                          "enabled": False, "new": 0}
    _enabled(monkeypatch)
    out = play.crawl(session, fetch=_fetch, now=NOW)
    assert out["seen"] == 4 and out["new"] == 2 and out["excluded"] == 2
    movie = play.existing_item(session, "6205267")
    assert play.is_play_item(movie) and movie.feed_name == "play"
    assert movie.title == "Tosts par mīlestību" and movie.section == "entertainment"
    assert play.play_data(movie)["genres"] == ["Romantiskā komēdija"]
    assert play.play_data(movie)["rating"] == "12+"
    assert movie.published_at == datetime(2026, 8, 30, 16, 0)
    ep = play.existing_item(session, "7426850")
    assert ep.title == "Klārksona ferma: 3. sērija"
    assert play.play_data(ep)["genres"] == ["Dokumentāls", "Humors"]          # no raidījuma lapas
    assert ep.images == ["https://tv3cdn.lv/p/kf-poster.jpg", "https://tv3cdn.lv/t/kf.jpg"]
    assert "TV3 Play sērija (45 min" in play.hint(ep)
    assert play.summary(session)["items"] == 2
    # otrreiz nedublē
    assert play.crawl(session, fetch=_fetch, now=NOW)["new"] == 0


def test_formats_windows_freshness_and_utm_for_play_items(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    _enabled(monkeypatch)
    play.crawl(session, fetch=_fetch, now=NOW)
    movie = play.existing_item(session, "6205267")
    from app import formats
    from app.best_practices import add_utm
    from app.pipeline import utm_campaign

    assert formats.suitable_formats(movie, ["link", "photo", "card_carousel", "reel", "story"]) == \
        ["link", "photo", "story"]
    v = evaluate(movie, "fb_play", {"formats": ["link"]}, config.load_rules(), NOW)
    assert v.outcome == "eligible"
    assert [(w[0].hour, w[1].hour) for w in v.allowed_windows] == [(19, 22)]   # vakara logs
    assert v.fresh_until == movie.published_at + timedelta(hours=240)
    # 16+ tikai vēlu vakarā
    movie.raw_json = {**movie.raw_json, "_play": {**play.play_data(movie), "rating": "16+"}}
    v = evaluate(movie, "fb_play", {"formats": ["link"]}, config.load_rules(), NOW)
    assert [(w[0].hour, w[1].hour) for w in v.allowed_windows] == [(21, 23)]
    post = Post(article_id=movie.id, channel="fb_play", format="link", copy="c",
                link_url=movie.url, state="scheduled", extra={})
    session.add(post)
    session.flush()
    assert utm_campaign(post) == "play"
    assert "utm_campaign=play" in add_utm(post.link_url, "facebook_page", post.id,
                                          campaign=utm_campaign(post))


def test_ethics_guards_block_play_next_to_tragedy_and_on_somber_days(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    _enabled(monkeypatch)
    play.crawl(session, fetch=_fetch, now=NOW)
    movie = play.existing_item(session, "6205267")   # romantiskā komēdija
    ep = play.existing_item(session, "7426850")      # dokumentāls, humors

    # parasta diena: divi neitrāli ieraksti, viens traģisks — 33 % < 40 %
    _news(session, "n1", "Saeima pieņem budžetu", at=NOW - timedelta(hours=2))
    _news(session, "n2", "Rīgā atklāj jaunu skolu", at=NOW - timedelta(hours=1))
    grim_a, grim_p = _news(session, "n3", "Avārijā uz Tallinas šosejas gājis bojā cilvēks",
                           sensitivity=["tragedy"], at=NOW + timedelta(minutes=30))
    session.commit()
    assert play.is_grim(grim_a) is True
    ok, why = play.allowed_now(session, movie, "fb_play", now=NOW)
    assert ok, why

    # attālums plūsmā: slots blakus traģēdijai tiek noraidīts, 2 h vēlāk der
    cfg = {"platform": "facebook_page", "min_gap_minutes": 15, "formats": ["link", "photo"]}
    v = Verdict("eligible", earliest=NOW, fresh_until=NOW + timedelta(days=3))
    queue = slots._channel_queue(session, "fb_play", NOW)
    assert play.too_close_to_grim(queue, NOW + timedelta(minutes=45)).startswith("Play pārāk tuvu")
    assert play.too_close_to_grim(queue, NOW + timedelta(hours=3)) == ""
    slot, why = slots.plan_slot(session, "fb_play", cfg, v, "entertainment", "link",
                                movie.title, NOW, promo=True)
    assert slot is not None and slot - grim_p.scheduled_at >= timedelta(minutes=90)

    # drūma diena: vēl divas traģēdijas -> 60 %. Asa sižeta filma un nosaukums
    # bez žanra paliek; komēdija un dokumentālais drīkst
    _news(session, "n4", "Ugunsgrēkā Liepājā gājuši bojā divi cilvēki", sensitivity=["tragedy"],
          at=NOW - timedelta(minutes=30))
    _news(session, "n5", "Slepkavība Daugavpilī", sensitivity=["crime"], at=NOW - timedelta(minutes=10))
    session.commit()
    is_somber, share = play.somber(session, NOW)
    assert is_somber and share >= 0.4
    action = Article(guid="play:555", url="https://play.tv3.lv/filmas/trieciens-555/",
                     canonical_url="https://play.tv3.lv/filmas/trieciens-555/", title="Trieciens",
                     section="entertainment", feed_name="play",
                     raw_json={"_play": {"kind": "movie", "show": "trieciens", "show_id": "555",
                                         "genres": ["Asa sižeta", "Trilleris"]}})
    unknown = Article(guid="play:556", url="https://play.tv3.lv/filmas/x-556/",
                      canonical_url="https://play.tv3.lv/filmas/x-556/", title="X",
                      section="entertainment", feed_name="play",
                      raw_json={"_play": {"kind": "movie", "show": "x", "show_id": "556", "genres": []}})
    session.add_all([action, unknown])
    session.flush()
    for a in (action, unknown):
        ok, why = play.allowed_now(session, a, "fb_play", now=NOW)
        assert not ok and "drūma diena" in why
    for a in (movie, ep):
        ok, why = play.allowed_now(session, a, "fb_play", now=NOW)
        assert ok, why
    # pauze ar roku pārtrauc visu
    set_setting(session, "play:pause", "on")
    assert play.allowed_now(session, ep, "fb_play", now=NOW) == (False, "Play pauzēts ar roku")
    set_setting(session, "play:pause", "")
    assert play.summary(session, now=NOW)["somber"] is True


def test_cooldown_daily_cap_and_feed_share(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    _enabled(monkeypatch)
    play.crawl(session, fetch=_fetch, now=NOW)
    ep = play.existing_item(session, "7426850")
    movie = play.existing_item(session, "6205267")
    for i in range(30):   # pilna ziņu nedēļa, lai daļas sargs ir dzīvs (10 % = 3 Play)
        _news(session, f"w{i}", f"Ziņa numur {i} par notikumu",
              at=NOW - timedelta(days=i % 6, hours=1 + i % 5))
    # tā paša raidījuma cita sērija vakar -> atdzišana
    ep2 = Article(guid="play:7426851", url="https://play.tv3.lv/video/klarksona-ferma-7426847/serija-4-7426851/",
                  canonical_url="https://play.tv3.lv/video/klarksona-ferma-7426847/serija-4-7426851/",
                  title="Klārksona ferma: 4. sērija", section="entertainment", feed_name="play",
                  raw_json={"_play": {"kind": "episode", "show": "klarksona-ferma", "show_id": "7426847",
                                      "genres": ["Dokumentāls"]}})
    session.add(ep2)
    session.flush()
    session.add(Post(article_id=ep2.id, channel="fb_play", format="link", copy="c",
                     scheduled_at=NOW - timedelta(days=1), state="published", extra={}))
    session.commit()
    ok, why = play.allowed_now(session, ep, "fb_play", now=NOW)
    assert not ok and "jau bija" in why
    # filma šodien: pirmā der, otra pārsniedz darbdienas limitu 1
    ok, why = play.allowed_now(session, movie, "fb_play", now=NOW)
    assert ok, why
    session.add(Post(article_id=movie.id, channel="fb_play", format="photo", copy="c",
                     scheduled_at=NOW + timedelta(hours=1), state="scheduled", extra={}))
    session.commit()
    other = Article(guid="play:999", url="https://play.tv3.lv/filmas/cita-999/",
                    canonical_url="https://play.tv3.lv/filmas/cita-999/", title="Cita filma",
                    section="entertainment", feed_name="play",
                    raw_json={"_play": {"kind": "movie", "show": "cita", "show_id": "999", "genres": ["Drāma"]}})
    session.add(other)
    session.flush()
    ok, why = play.allowed_now(session, other, "fb_play", now=NOW)
    assert not ok and "dienas limits" in why
    # stāstam savs limits
    ok, why = play.allowed_now(session, other, "fb_play", fmt="story", now=NOW)
    assert ok, why
    # plūsmas daļa: ar 5 Play no 35 (atļauti 4) rītdienas promo tiek apturēts
    for j in range(3):
        session.add(Post(article_id=ep2.id, channel="fb_play", format="link", copy="c",
                         scheduled_at=NOW - timedelta(days=2 + j), state="published", extra={}))
    session.commit()
    ok, why = play.allowed_now(session, other, "fb_play", now=NOW + timedelta(days=1))
    assert not ok and "plūsmas" in why


def test_diagnostics_shows_play_block_and_pause_toggle(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    client.post("/setup", data={"password": "slepens123", "password2": "slepens123"})
    body = client.get("/logs").text
    assert "TV3 Play (play.tv3.lv)" in body and "IZSLĒGTS" in body
    client.post("/toggle/play-pause")
    assert play.paused(session) is True
    assert "pauzēts ar roku" in client.get("/logs").text
