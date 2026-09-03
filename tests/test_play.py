"""TV3 Play promo: katalogs, ētikas sargi, kvotas, logs, izlases, tilti, UTM."""
from datetime import datetime, timedelta

from app import config, play, slots
from app.models import Article, Post, set_setting, utcnow
from app.rules_engine import Verdict, evaluate

SITEMAP = """<?xml version="1.0"?><urlset xmlns:video="http://www.google.com/schemas/sitemap-video/1.1">
<url><loc>https://play.tv3.lv/filmas/kinozvaigzne-un-kovbojs-6689723/</loc><lastmod>2026-08-30T20:00:00+03:00</lastmod>
<video:video><video:thumbnail_loc>https://tv3cdn.lv/t/kino.jpg</video:thumbnail_loc><video:title>Kinozvaigzne un kovbojs</video:title>
<video:player_loc>https://play.tv3.lv/goTo/6689723</video:player_loc><video:publication_date>2026-08-30T19:00:00+03:00</video:publication_date>
<video:duration>5253</video:duration><video:description>Romantiska komēdija.</video:description></video:video></url>
<url><loc>https://play.tv3.lv/video/nemiletie-1915103/serija-62-10707122/</loc><lastmod>2026-08-28T20:00:00+03:00</lastmod>
<video:video><video:thumbnail_loc>https://tv3cdn.lv/t/nem.jpg</video:thumbnail_loc><video:title>Sērija 62. Fināls</video:title>
<video:player_loc>https://play.tv3.lv/goTo/10707122</video:player_loc><video:publication_date>2026-08-28T19:00:00+03:00</video:publication_date>
<video:duration>2640</video:duration><video:description>Sezonas noslēgums.</video:description></video:video></url>
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

BROWSE = """<html><body><div class="grid">
<a href="/video/klarksona-ferma-7426847/">Klārksona ferma</a>
<a href="/video/nemiletie-1915103/">Nemīlētie</a>
<a href="https://play.tv3.lv/filmas/kinozvaigzne-un-kovbojs-6689723/">Kinozvaigzne un kovbojs</a>
<a href="/video/tv3-zinas-2530780/">TV3 Ziņas</a>
<a href="/video/klarksona-ferma-7426847/serija-3-7426850/">3. sērija</a>
<a href="/filmas/">Filmas</a><a href="/meklet/">Meklēt</a>
</div></body></html>"""

# Īsto Play lapu metadati (Diagnostikas zonde 07.09.2026): žanri un plakāti ir
# cXense tagos, og:title nes sadaļu aiz svītras, vecuma cenza lapā NAV.
SHOW_PAGE = """<html><head>
<meta property="og:type" content="video.tv_show">
<meta property="og:title" content="Klārksona ferma | Šovi un raidījumi">
<meta property="og:image" content="https://tv3cdn.lv/thumbnails/1200x630/go3/serial/7426847/og.jpg">
<meta name="cXenseParse:zfv-playProductTitle" content="Klārksona ferma">
<meta name="cXenseParse:zfv-playProductGenre" content="Dokumentālās">
<meta name="cXenseParse:zfv-playProductGenre" content="Komēdijas">
<meta name="cXenseParse:zfv-playProductCategories" content="documentary">
<meta name="cXenseParse:zfv-playProductCategories" content="comedy">
<meta name="cXenseParse:zfv-playProductImage3x4" content="https://tv3cdn.lv/thumbnails/468x624/go3/serial/7426847/poster.jpg">
<meta name="cXenseParse:zfv-playProductImage16x9" content="https://tv3cdn.lv/thumbnails/593x336/go3/serial/7426847/wide.jpg">
<meta name="cXenseParse:zfv-playProductYear" content="2024">
<script type="application/ld+json">{"@type":"TVSeries","name":"Klārksona ferma",
"description":"Džeremijs Klārksons mācās vadīt fermu Anglijas laukos."}</script>
</head><body></body></html>"""

MOVIE_PAGE = """<html><head>
<meta property="og:type" content="video.movie">
<meta property="og:title" content="Kinozvaigzne un kovbojs | Filmas">
<meta property="og:image" content="https://tv3cdn.lv/thumbnails/1200x630/go3/vod/6689723/og.jpg">
<meta property="video:duration" content="5253">
<meta property="video:release_date" content="2026-07-26T20:01:59+03:00">
<meta name="cXenseParse:zfv-playProductTitle" content="Kinozvaigzne un kovbojs">
<meta name="cXenseParse:zfv-playProductGenre" content="Komēdijas">
<meta name="cXenseParse:zfv-playProductGenre" content="Drāmas">
<meta name="cXenseParse:zfv-playProductGenre" content="Romantika">
<meta name="cXenseParse:zfv-playProductCategories" content="drama">
<meta name="cXenseParse:zfv-playProductCategories" content="romance">
<meta name="cXenseParse:zfv-playProductImage3x4" content="https://tv3cdn.lv/thumbnails/468x624/go3/vod/6689723/poster.jpg">
<meta name="cXenseParse:zfv-playProductImage16x9" content="https://tv3cdn.lv/thumbnails/593x336/go3/vod/6689723/wide.jpg">
<meta name="cXenseParse:zfv-playProductYear" content="2023">
<meta name="cXenseParse:zfv-playProductLabeloriginalTitle" content="{&quot;text&quot;:&quot;The Movie Star and the Cowboy&quot;}">
<script type="application/ld+json">{"@type":"VideoObject","name":"Kinozvaigzne un kovbojs",
"description":"Kad Izabellai rodas iespēja iegūt kovbojmeitenes lomu, viņa ir gatava darīt visu.",
"duration":"PT1H27M33S","embedUrl":"https://play.tv3.lv/embed-video/kinozvaigzne-un-kovbojs,vod-6689723"}</script>
</head><body>
<span class="label label-last">Pēdējā iespēja</span>
<p class="availability">Pieejams vēl 3 dienas</p>
</body></html>"""

NOW = datetime(2026, 9, 3, 17, 0)   # trešdiena 20:00 Rīgā

SERIES_PAGE = """<html><head>
<meta property="og:type" content="video.tv_show">
<meta property="og:title" content="Nemīlētie | Seriāli">
<meta name="cXenseParse:zfv-playProductTitle" content="Nemīlētie">
<meta name="cXenseParse:zfv-playProductGenre" content="Drāmas">
<meta name="cXenseParse:zfv-playProductCategories" content="drama">
<meta name="cXenseParse:zfv-playProductImage3x4" content="https://tv3cdn.lv/thumbnails/468x624/go3/serial/1915103/poster.jpg">
<meta name="cXenseParse:zfv-playProductSeasonsCount" content="10">
</head><body><span class="label label-season">10. SEZONA - FINĀLS</span></body></html>"""

PAGES = {"https://play.tv3.lv/sitemaps/sitemap-latest.xml": SITEMAP,
         "https://play.tv3.lv/video/nemiletie-1915103/": SERIES_PAGE,
         "https://play.tv3.lv/sitemaps/sitemap-2026-09.xml": "",
         "https://play.tv3.lv/": BROWSE,
         "https://play.tv3.lv/video/klarksona-ferma-7426847/": SHOW_PAGE,
         "https://play.tv3.lv/filmas/kinozvaigzne-un-kovbojs-6689723/": MOVIE_PAGE}


def _fetch(url, timeout=10):
    return PAGES.get(url, "")


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
    # filma nāk no sadaļu lapas, ilgumu un sīktēlu pieliek sitemap
    assert kinds["6689723"]["kind"] == "movie" and kinds["6689723"]["source"] == "browse"
    assert kinds["6689723"]["seconds"] == 5253
    assert kinds["6689723"]["thumbnail"] == "https://tv3cdn.lv/t/kino.jpg"
    assert kinds["7426850"]["kind"] == "episode" and kinds["7426850"]["show"] == "klarksona-ferma"
    assert kinds["7426850"]["show_id"] == "7426847"
    assert "tiesraides" not in " ".join(i["kind"] for i in items)
    assert play.excluded(kinds["12292095"], cfg).startswith("ziņu raidījums")
    assert play.excluded(kinds["12084795"], cfg).startswith("ziņu raidījums")
    assert play.excluded({"kind": "episode", "show": "kaut-kas", "seconds": 60}, cfg).startswith("par īsu")
    assert play.excluded(kinds["6689723"], cfg) == ""


def test_crawl_is_off_by_default_and_builds_rows_with_genres_when_on(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    assert play.crawl(session, fetch=_fetch, now=NOW) == {**play.crawl(session, fetch=_fetch, now=NOW),
                                                          "enabled": False, "new": 0}
    _enabled(monkeypatch)
    out = play.crawl(session, fetch=_fetch, now=NOW)
    assert out["seen"] == 8 and out["new"] == 5 and out["excluded"] == 3
    movie = play.existing_item(session, "6689723")
    assert play.is_play_item(movie) and movie.feed_name == "play"
    # nosaukums no cXense (bez « | Filmas»), žanri latviski + kategorijas angliski
    assert movie.title == "Kinozvaigzne un kovbojs" and movie.section == "entertainment"
    data = play.play_data(movie)
    assert data["genres"] == ["Komēdijas", "Drāmas", "Romantika"]
    assert data["categories"] == ["drama", "romance"]
    assert data["year"] == 2023 and data["seconds"] == 5253
    # Play vecuma cenzu nedod: rating tukšs, pieaugušo saturs pēc adreses
    assert data["rating"] == "" and data["adult"] is False
    # vertikālais plakāts pirmais — stāstiem un foto
    assert movie.images[0].endswith("468x624/go3/vod/6689723/poster.jpg")
    assert "Izabellai rodas iespēja" in movie.lead
    assert movie.published_at == datetime(2026, 8, 30, 16, 0)
    ep = play.existing_item(session, "7426850")
    assert ep.title == "Klārksona ferma: 3. sērija"
    assert play.play_data(ep)["genres"] == ["Dokumentālās", "Komēdijas"]      # no raidījuma lapas
    assert ep.images[0].endswith("468x624/go3/serial/7426847/poster.jpg")
    assert "TV3 Play sērija (45 min" in play.hint(ep)
    show = play.existing_item(session, "7426847")
    assert show.title == "Klārksona ferma" and play.play_data(show)["kind"] == "show"
    assert play.summary(session)["items"] == 5
    # otrreiz nedublē
    assert play.crawl(session, fetch=_fetch, now=NOW)["new"] == 0


def test_formats_windows_freshness_and_utm_for_play_items(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    _enabled(monkeypatch)
    play.crawl(session, fetch=_fetch, now=NOW)
    movie = play.existing_item(session, "6689723")
    from app import formats
    from app.best_practices import add_utm
    from app.pipeline import utm_campaign

    assert formats.suitable_formats(movie, ["link", "photo", "card_carousel", "reel", "story"]) == \
        ["link", "photo", "story"]
    v = evaluate(movie, "fb_play", {"formats": ["link"]}, config.load_rules(), NOW)
    assert v.outcome == "eligible"
    assert [(w[0].hour, w[1].hour) for w in v.allowed_windows] == [(19, 22)]   # vakara logs
    # kataloga nosaukums nenoveco kā ziņa — svaiguma griestu nav
    assert v.fresh_until is None
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
    movie = play.existing_item(session, "6689723")   # romantiskā komēdija
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
    movie = play.existing_item(session, "6689723")
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


def _third_show(session):
    a = Article(guid="play:8888", url="https://play.tv3.lv/video/mana-ferma-8888/",
                canonical_url="https://play.tv3.lv/video/mana-ferma-8888/", title="Mana ferma",
                section="entertainment", feed_name="play", images=["https://tv3cdn.lv/p/mf.jpg"],
                published_at=NOW - timedelta(days=2),
                raw_json={"_play": {"kind": "show", "show": "mana-ferma", "show_id": "8888",
                                    "genres": ["Realitātes šovs"], "seconds": 2400}})
    session.add(a)
    session.flush()
    return a


def test_selection_carousel_is_built_on_selection_days_and_waits_for_approval(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    _enabled(monkeypatch)
    play.crawl(session, fetch=_fetch, now=NOW)
    from app import cards

    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    rendered = {}

    def fake_cards(title, section, tag, points, image, question, **kw):
        rendered.update(title=title, points=points, subtitles=kw.get("point_dates"),
                        images=kw.get("point_images"), label=kw.get("label"))
        return [f"data/cards/p{i}.png" for i in range(len(points))]

    monkeypatch.setattr(cards, "render_cards", fake_cards)
    friday = datetime(2026, 9, 4).date()
    _third_show(session)
    session.commit()
    post = play.build_selection(session, friday, NOW)
    assert post is not None and post.state == "proposed" and post.hook_type == "playselection"
    assert post.channel == "fb_tv3lv" and post.format == "card_carousel"
    assert rendered["title"] == "Piektdienas vakaram: TV3 Play"
    assert rendered["label"] == "TV3 PLAY · BEZ MAKSAS"
    assert set(rendered["points"]) == {"Kinozvaigzne un kovbojs", "Klārksona ferma",
                                       "Nemīlētie", "Mana ferma"}
    assert any("Komēdijas · 87 min · pēdējā iespēja" == s for s in rendered["subtitles"])
    # sērija ved uz raidījuma lapu, katra kartīte ar savu saiti
    links = post.extra["card_links"]
    assert "https://play.tv3.lv/video/klarksona-ferma-7426847/" in links
    assert len(links) == 4 and len(post.extra["items"]) == 4
    assert post.extra["items"][0]["show_id"]
    assert post.extra["timeless"] is True
    # slots vakara logā Rīgā (19:30 = 16:30 UTC)
    assert post.scheduled_at == datetime(2026, 9, 4, 16, 30)
    # pirmais komentārs ar Play saitēm un utm_campaign=play
    from app import pipeline

    comment = pipeline.first_comment_text(post, "facebook_page", "https://x")
    assert "utm_campaign=play" in comment and "klarksona-ferma" in comment
    # tick to nebūvē otrreiz, un atzīmē dienu
    from app.models import get_setting

    assert get_setting(session, "play:selection:2026-09-04") == str(post.id)
    monkeypatch.setattr(play, "crawl", lambda s, now=None: {"new": 0})
    assert play.tick(session, datetime(2026, 9, 4, 15, 0))["selection"] is None


def test_selection_moves_away_from_a_tragedy_and_skips_on_somber_days(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    _enabled(monkeypatch)
    play.crawl(session, fetch=_fetch, now=NOW)
    _third_show(session)
    from app import cards

    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    monkeypatch.setattr(cards, "render_cards",
                        lambda *a, **k: [f"data/cards/p{i}.png" for i in range(len(a[3]))])
    friday = datetime(2026, 9, 4).date()
    slot = datetime(2026, 9, 4, 16, 30)
    _news(session, "g1", "Traģēdija Rīgā: gājis bojā bērns", sensitivity=["tragedy"], at=slot,
          channel="fb_tv3lv")
    _news(session, "g2", "Saeima lemj par budžetu", at=slot - timedelta(hours=3), channel="fb_tv3lv")
    _news(session, "g3", "Jauna skola Mārupē", at=slot - timedelta(hours=2), channel="fb_tv3lv")
    session.commit()
    post = play.build_selection(session, friday, datetime(2026, 9, 4, 14, 0))
    assert post is not None and post.scheduled_at - slot >= timedelta(minutes=90)


def test_bridge_links_positive_entertainment_articles_to_play_titles(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    _enabled(monkeypatch)
    play.crawl(session, fetch=_fetch, now=NOW)
    from app import pipeline

    art = Article(guid="br-1", url="https://tv3.lv/izklaide/klarksona-ferma-jauna-sezona/",
                  canonical_url="https://tv3.lv/izklaide/klarksona-ferma-jauna-sezona/",
                  title="Klārksona ferma atgriežas ar jaunu sezonu", section="entertainment",
                  images=["https://cdn/k.jpg"], raw_json={})
    grim = Article(guid="br-2", url="https://tv3.lv/zinas/x/", canonical_url="https://tv3.lv/zinas/x/",
                   title="Klārksona ferma: traģēdija uzņemšanas laukumā, gājis bojā operators",
                   section="entertainment", sensitivity=["tragedy"], raw_json={})
    session.add_all([art, grim])
    session.flush()
    assert play.bridge_for_article(session, grim, NOW) is None
    bridge = play.bridge_for_article(session, art, NOW)
    assert bridge and bridge["title"] == "Klārksona ferma"
    assert bridge["url"] == "https://play.tv3.lv/video/klarksona-ferma-7426847/"
    post = Post(article_id=art.id, channel="fb_tv3lv", format="link", copy="Jauna sezona!",
                hashtags=[], link_url=art.url, state="scheduled", extra={})
    session.add(post)
    session.flush()
    text, in_comment = pipeline.compose_text(post, "facebook_page", "https://tv3.lv/izklaide/x/")
    assert "Skaties «Klārksona ferma» bez maksas TV3 Play" in text and "utm_campaign=play" in text
    # X: tilta rindas tekstā nav (otra saite 280 zīmēs)
    text_x, _ = pipeline.compose_text(post, "x", "https://tv3.lv/izklaide/x/")
    assert "TV3 Play" not in text_x
    # foto ierakstam tilts iet pirmajā komentārā
    photo = Post(article_id=art.id, channel="fb_tv3lv", format="photo", copy="c", hashtags=[],
                 link_url=art.url, state="scheduled", extra={})
    session.add(photo)
    session.flush()
    assert "TV3 Play" in pipeline.first_comment_text(photo, "facebook_page", "https://tv3.lv/izklaide/x/")
    # atdzišana: tas pats raidījums 3 dienas netiek tiltots atkārtoti
    again = Article(guid="br-3", url="https://tv3.lv/izklaide/y/", canonical_url="https://tv3.lv/izklaide/y/",
                    title="Klārksona ferma: Džeremijs pērk jaunu traktoru", section="entertainment", raw_json={})
    session.add(again)
    session.flush()
    assert play.bridge_for_article(session, again, NOW + timedelta(days=1)) is None
    assert play.bridge_for_article(session, again, NOW + timedelta(days=4)) is not None
    # ziņu sadaļa netiek tiltota
    news = Article(guid="br-4", url="https://tv3.lv/zinas/z/", canonical_url="https://tv3.lv/zinas/z/",
                   title="Klārksona ferma un lauksaimniecības politika", section="news", raw_json={})
    session.add(news)
    session.flush()
    assert play.bridge_for_article(session, news, NOW + timedelta(days=9)) is None


def test_paid_boost_takes_only_organically_working_play_posts_within_its_share(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    _enabled(monkeypatch)
    play.crawl(session, fetch=_fetch, now=NOW)
    from app import ads
    from app.models import PostMetrics

    movie = play.existing_item(session, "6689723")
    ep = play.existing_item(session, "7426850")
    strong = Post(article_id=movie.id, channel="fb_tv3lv", format="photo", copy="c",
                  link_url=movie.url, state="published", published_at=NOW - timedelta(hours=5),
                  media=["data/cards/m.png"], extra={})
    weak = Post(article_id=ep.id, channel="fb_tv3lv", format="link", copy="c",
                link_url=ep.url, state="published", published_at=NOW - timedelta(hours=6), extra={})
    session.add_all([strong, weak])
    session.flush()
    session.add(PostMetrics(post_id=strong.id, impressions=2500, clicks=40, ga_sessions=30))
    session.add(PostMetrics(post_id=weak.id, impressions=120, clicks=1, ga_sessions=1))
    # parasts raksts konkurencei
    news = Article(guid="ad-n", url="https://tv3.lv/sports/n/", canonical_url="https://tv3.lv/sports/n/",
                   title="Sporta ziņa", section="sport", ai_score=0.8, raw_json={"_boostable": True})
    session.add(news)
    session.flush()
    session.add(Post(article_id=news.id, channel="fb_tv3lv", format="link", copy="c",
                     link_url=news.url, state="published", published_at=NOW - timedelta(hours=2), extra={}))
    session.commit()
    ads.save_settings(session, "approve", 100.0, 0, 0)
    picked, rejected = ads.candidates(session, NOW)
    by_post = {e["post"].id: e for e in picked}
    assert strong.id in by_post and by_post[strong.id]["play"] is True
    assert by_post[strong.id]["score"] == 16.0                      # max(30 sesijas, 40 klikšķi) / 2500 * 1000
    assert weak.id not in by_post
    assert any(e["post"].id == weak.id and "slieksnis" in e["reason"] for e in rejected)
    plan = ads.build_plan(session, NOW)
    play_rows = [r for r in plan["planned"] if r.get("play")]
    news_rows = [r for r in plan["planned"] if not r.get("play") and r["objective"] == "traffic"]
    assert len(play_rows) == 1 and play_rows[0]["budget_eur"] == 15.0   # 15 % no 100 €
    assert news_rows and news_rows[0]["budget_eur"] == 85.0
    # drūma diena aptur arī reklāmu
    for i in range(3):
        _news(session, f"s{i}", "Slepkavība un traģēdija", sensitivity=["tragedy"],
              at=NOW - timedelta(minutes=10 * i), channel="fb_tv3lv")
    session.commit()
    monkeypatch.setattr(play, "somber", lambda s, now=None, rules=None: (True, 0.6))
    picked, rejected = ads.candidates(session, NOW)
    assert all(not e.get("play") for e in picked)


def test_adult_titles_are_recognised_by_slug_and_deferred_when_page_budget_runs_out(
        session, monkeypatch):
    """Play lapas vecuma cenzu nedod (zonde 07.09.2026), tāpēc pieaugušo saturu
    šķiro pēc adreses; bez lapas ielasīšanas nosaukumam nav ne žanra, ne
    plakāta, tāpēc tas gaida nākamo apgājienu."""
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    cfg = play.settings({})
    assert play.is_adult({"url": "https://play.tv3.lv/video/taizeme-tikai-pieaugusajiem-5897873/"},
                         cfg) is True
    assert play.is_adult({"categories": ["erotic"]}, cfg) is True
    assert play.is_adult({"rating": "16+"}, cfg) is True
    assert play.is_adult({"show": "klarksona-ferma", "genres": ["Komēdijas"]}, cfg) is False
    # adrese ar pieaugušo pazīmi -> vēlais logs
    adult = Article(guid="play:5897873",
                    url="https://play.tv3.lv/video/taizeme-tikai-pieaugusajiem-5897873/",
                    canonical_url="https://play.tv3.lv/video/taizeme-tikai-pieaugusajiem-5897873/",
                    title="Taizeme tikai pieaugušajiem", section="entertainment",
                    feed_name="play", raw_json={"_play": {"kind": "show", "show":
                                                          "taizeme-tikai-pieaugusajiem",
                                                          "show_id": "5897873", "adult": True}})
    session.add(adult)
    session.flush()
    assert play.windows_for(adult) == ["21:00-23:59"]

    _enabled(monkeypatch, page_fetch_per_run=1)
    out = play.crawl(session, fetch=_fetch, now=NOW)
    assert out["new"] == 1 and out["deferred"] >= 1
    # nākamais apgājiens paņem atlikušos
    assert play.crawl(session, fetch=_fetch, now=NOW)["new"] >= 1


def test_availability_window_drives_last_chance_and_blocks_removed_titles(session, monkeypatch):
    """Nosaukuma lapā ir atskaite «Pieejams vēl 3 dienas» un birka «Pēdējā
    iespēja» — kataloga notikums izlasēm un vienlaikus derīguma termiņš."""
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    assert play.availability("<p>Pieejams vēl 3 dienas</p>") == {"expires_days": 3,
                                                                 "last_chance": False}
    assert play.availability("<span>Pēdējā iespēja</span><p>Pieejams vēl 12 dienas</p>") == {
        "expires_days": 12, "last_chance": True}
    assert play.availability("<p>Skaties bez maksas</p>") == {"expires_days": None,
                                                              "last_chance": False}

    _enabled(monkeypatch)
    play.crawl(session, fetch=_fetch, now=NOW)
    movie = play.existing_item(session, "6689723")
    data = play.play_data(movie)
    assert data["last_chance"] is True
    assert data["original_title"] == "The Movie Star and the Cowboy"
    assert data["embed"].endswith("vod-6689723")
    assert play.last_chance(movie) is True
    assert "PĒDĒJĀ IESPĒJA" in play.hint(movie)
    ep = play.existing_item(session, "7426850")
    assert play.last_chance(ep) is False          # raidījumam termiņa nav

    # kad termiņš pagājis, saite vestu uz «nav pieejams» — ierakstu nelaižam
    assert play.expired(movie) is False
    ok, _ = play.allowed_now(session, movie, "fb_play", now=NOW)
    assert ok
    later = utcnow() + timedelta(days=4)
    assert play.expired(movie, later) is True
    assert play.allowed_now(session, movie, "fb_play", now=later) == (
        False, "nosaukums Play vairs nav pieejams")
    assert play.summary(session, now=NOW)["items_last_chance"] == 1


def test_last_chance_titles_lead_the_selection_and_carry_the_badge(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    _enabled(monkeypatch)
    play.crawl(session, fetch=_fetch, now=NOW)
    _third_show(session)
    from app import cards

    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    rendered = {}

    def fake_cards(title, section, tag, points, image, question, **kw):
        rendered.update(points=points, subtitles=kw.get("point_dates"))
        return [f"data/cards/p{i}.png" for i in range(len(points))]

    monkeypatch.setattr(cards, "render_cards", fake_cards)
    post = play.build_selection(session, datetime(2026, 9, 4).date(), NOW)
    assert post is not None
    # «pēdējā iespēja» ir pirmā kartīte, un tas redzams arī uz tās
    # «pēdējā iespēja» ir steidzamāka par finālu, tāpēc pirmā
    assert rendered["points"][0] == "Kinozvaigzne un kovbojs"
    assert rendered["subtitles"][0] == "Komēdijas · 87 min · pēdējā iespēja"
    assert rendered["points"][1] == "Nemīlētie"


def test_season_finale_is_recognised_as_a_catalogue_event(session, monkeypatch):
    """Sērijas lapā ir birka «10. SEZONA - FINĀLS» — spēcīgākais iemesls
    ierakstam tieši šodien. Sērijas notikums nāk no tās nosaukuma, sezona no
    raidījuma lapas; raidījuma birka nepieder katrai sērijai."""
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    assert play.labels("<span>10. SEZONA - FINĀLS</span>") == {"season": 10, "event": "finale"}
    assert play.labels("Jauna sezona jau 5. septembrī") == {"season": None,
                                                            "event": "new_season"}
    assert play.labels("Pirmizrāde") == {"season": None, "event": "premiere"}
    assert play.labels("Parasta sērija") == {"season": None, "event": ""}

    _enabled(monkeypatch)
    play.crawl(session, fetch=_fetch, now=NOW)
    finale = play.existing_item(session, "10707122")
    assert finale.title == "Nemīlētie: Sērija 62. Fināls"
    data = play.play_data(finale)
    assert data["event"] == "finale" and data["season"] == 10
    assert play.event_label(finale) == "10. sezonas fināls"
    assert "NOTIKUMS: 10. SEZONAS FINĀLS" in play.hint(finale)
    # cita tā paša raidījuma sērija notikumu nemanto
    plain = play.existing_item(session, "7426850")
    assert play.play_data(plain)["event"] == "" and play.event_label(plain) == ""
    # notikumu nes gan raidījuma lapa (birka), gan pati fināla sērija
    show = play.existing_item(session, "1915103")
    assert play.event_label(show) == "10. sezonas fināls"
    assert play.summary(session, now=NOW)["items_events"] == 2


def test_events_lead_the_selection_and_show_on_the_card(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    _enabled(monkeypatch)
    play.crawl(session, fetch=_fetch, now=NOW)
    from app import cards

    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    rendered = {}

    def fake_cards(title, section, tag, points, image, question, **kw):
        rendered.update(points=points, subtitles=kw.get("point_dates"))
        return [f"data/cards/p{i}.png" for i in range(len(points))]

    monkeypatch.setattr(cards, "render_cards", fake_cards)
    post = play.build_selection(session, datetime(2026, 9, 4).date(), NOW)
    assert post is not None
    # notikumi (fināls, pēdējā iespēja) ir pirmās kartītes, un tas redzams uz tām
    assert rendered["points"][:2] == ["Kinozvaigzne un kovbojs", "Nemīlētie"]
    lead = dict(zip(rendered["points"], rendered["subtitles"]))
    assert lead["Nemīlētie"].endswith("10. sezonas fināls")
    assert lead["Kinozvaigzne un kovbojs"].endswith("pēdējā iespēja")


def test_metadata_audit_reports_field_coverage_and_guard_consequences(session, monkeypatch):
    """Audits aizstāj ekrānuzņēmumu sūtīšanu: paraugi no katras sadaļas, lauku
    pārklājums, žanru vārdnīca un ko tā nozīmē drūmās dienas sargam."""
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    bare = """<html><head><meta property="og:title" content="Bez žanra | Filmas">
    <meta name="cXenseParse:zfv-playProductTitle" content="Bez žanra"></head><body></body></html>"""
    # žanra filtra lapa: produkta lauku nav, toties tajā ir īsts nosaukums
    listing = """<html><head><meta property="og:title" content="Filmas – Romantika">
    <meta property="og:type" content="website"></head><body>
    <a href="/filmas/kinozvaigzne-un-kovbojs-6689723/">A</a></body></html>"""
    # izceltā josla ir visās sadaļās — paraugā tā nedrīkst nonākt
    chrome = '<a href="/video/bez-tabu-2503775/">Bez Tabu</a>'
    pages = {**PAGES,
             "https://play.tv3.lv/": PAGES["https://play.tv3.lv/"] + chrome,
             "https://play.tv3.lv/filmas/": f"""<html><body>{chrome}
                 <a href="/filmas/romance-4197766/">Romantika</a>
                 <a href="/filmas/bez-zanra-777/">B</a></body></html>""",
             "https://play.tv3.lv/seriali/": f"""<html><body>{chrome}
                 <a href="/video/nemiletie-1915103/">N</a></body></html>""",
             "https://play.tv3.lv/berniem/": "<html><body>nav nosaukumu</body></html>",
             "https://play.tv3.lv/filmas/romance-4197766/": listing,
             "https://play.tv3.lv/filmas/bez-zanra-777/": bare}

    def fetch(url, timeout=10):
        return pages.get(url, "")

    data = play.audit(fetch=fetch, per_section=2, episodes=1, now=NOW)
    by_path = {s["path"]: s for s in data["sections"]}
    assert by_path["/filmas/"]["titles_found"] == 2 and by_path["/filmas/"]["sampled"] == 2
    assert by_path["/filmas/"]["chrome_skipped"] == 1
    assert "https://play.tv3.lv/video/bez-tabu-2503775/" in data["chrome_links"]
    assert by_path["/berniem/"]["fetched"] is True and by_path["/berniem/"]["titles_found"] == 0
    assert by_path["/podkasti/"]["fetched"] is False        # lapa neatbild
    # sērijas paraugs no sitemap ar savu notikumu
    eps = by_path["sērijas (no sitemap)"]["samples"]
    assert eps[0]["event"] == "finale" and eps[0]["kind"] == "episode"

    # žanra filtra lapa netiek skaitīta kā nosaukums bez metadatiem
    assert data["listing_pages"] == ["https://play.tv3.lv/filmas/romance-4197766/"]
    assert any("filtra lapas" in w for w in data["warnings"])

    # lauku pārklājums un žanru vārdnīca
    assert data["sampled_total"] >= 3
    assert data["field_coverage"]["genres"]["pct"] < 100      # «Bez žanra» velk uz leju
    assert data["field_coverage"]["rating"]["count"] == 0     # cenza Play nedod
    assert data["genres"]["Komēdijas"] >= 1 and "drama" in data["categories"]
    # viens nosaukums vairākās sadaļās kopskaitā skaitās vienreiz; fināls ir
    # gan raidījuma lapai, gan pašai fināla sērijai
    assert data["events"]["finale"] == 2
    sampled = {x["url"] for s in data["sections"] for x in s["samples"]}
    assert len(sampled) - len(data["listing_pages"]) == data["sampled_total"]

    # ko tas nozīmē sargiem
    assert "https://play.tv3.lv/filmas/bez-zanra-777/" in data["titles_without_genre"]
    assert any("nav žanra" in w for w in data["warnings"])
    assert any("vecuma cenza" in w for w in data["warnings"])
    assert any("/berniem/" in w for w in data["warnings"])

    play.save_audit(session, data)
    saved = play.last_audit(session)
    assert saved["sampled"] == data["sampled_total"] and saved["warnings"] >= 3
    assert play.summary(session, now=NOW)["audit"]["sampled"] == data["sampled_total"]


def test_audit_route_is_on_the_diagnostics_page(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    from fastapi.testclient import TestClient

    from app import pagemeta
    from app.main import app

    monkeypatch.setattr(pagemeta, "fetch", lambda url, timeout=10: PAGES.get(url, ""))
    client = TestClient(app)
    client.post("/setup", data={"password": "slepens123", "password2": "slepens123"})
    body = client.get("/logs").text
    assert "Analizēt visu Play metadatus" in body
    data = client.get("/logs/play-audit", params={"per_section": 1, "episodes": 1}).json()
    assert data["sampled_total"] >= 1 and "field_coverage" in data
    assert "Metadatu audits" in client.get("/logs").text


def test_crawl_harvests_titles_from_genre_filter_pages_and_skips_news_shows(session, monkeypatch):
    """Audits parādīja: sadaļu lapās pirmās saites ved uz ŽANRA FILTRA lapām
    («Filmas – Romantika»), ne uz nosaukumiem, un starp nosaukumiem ir ziņu
    raidījumi. Filtra lapa nekļūst par ierakstu, bet no tās paņem nosaukumus
    un tās žanru; ziņu saturu šķiro pēc žanra, ne pēc slugu saraksta."""
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    _enabled(monkeypatch, page_fetch_per_run=12)
    listing = """<html><head><meta property="og:title" content="Filmas – Romantika">
    <meta property="og:type" content="website"></head><body>
    <a href="/filmas/bez-zanra-777/">Bez žanra</a></body></html>"""
    bare = """<html><head><meta property="og:type" content="video.movie">
    <meta name="cXenseParse:zfv-playProductTitle" content="Filma bez žanra">
    <meta property="video:duration" content="4800"></head><body></body></html>"""
    news = """<html><head><meta property="og:type" content="video.tv_show">
    <meta name="cXenseParse:zfv-playProductTitle" content="Zviedru Galds">
    <meta name="cXenseParse:zfv-playProductGenre" content="Ziņas">
    <meta name="cXenseParse:zfv-playProductCategories" content="news"></head><body></body></html>"""
    family = """<html><head><meta property="og:type" content="video.movie">
    <meta name="cXenseParse:zfv-playProductTitle" content="Varenais Ričards 2">
    <meta name="cXenseParse:zfv-playProductGenre" content="Bērniem &amp; ģimenei">
    <meta name="cXenseParse:zfv-playProductGenre" content="Animācijas"></head><body></body></html>"""
    pages = {**PAGES,
             "https://play.tv3.lv/": """<html><body>
                 <a href="/filmas/romance-4197766/">Romantika</a>
                 <a href="/video/zviedru-galds-11323551/">Zviedru Galds</a>
                 <a href="/filmas/varenais-ricards-2-6138645/">Ričards</a></body></html>""",
             "https://play.tv3.lv/filmas/romance-4197766/": listing,
             "https://play.tv3.lv/filmas/bez-zanra-777/": bare,
             "https://play.tv3.lv/video/zviedru-galds-11323551/": news,
             "https://play.tv3.lv/filmas/varenais-ricards-2-6138645/": family}

    def fetch(url, timeout=10):
        return pages.get(url, "")

    out = play.crawl(session, fetch=fetch, now=NOW)
    assert out["listings"] == 1
    # filtra lapa pati par ierakstu nekļūst
    assert play.existing_item(session, "4197766") is None
    # bet tajā atrastais nosaukums kļūst, un žanru manto no filtra lapas
    harvested = play.existing_item(session, "777")
    assert harvested is not None and harvested.title == "Filma bez žanra"
    assert play.play_data(harvested)["genres"] == ["Romantika"]
    # ziņu raidījums netiek ņemts, arī ja slugu sarakstā tā nav
    assert play.existing_item(session, "11323551") is None
    assert out["excluded"] >= 1
    # HTML entītija žanrā atšifrēta — citādi vārdnīcā tas būtu divreiz
    ricards = play.existing_item(session, "6138645")
    assert play.play_data(ricards)["genres"] == ["Bērniem & ģimenei", "Animācijas"]
    # un šie žanri drūmā dienā ir atļauti (saraksts sinhronizēts ar auditu)
    assert play.genre_ok_on_somber_day(ricards) is True


def test_audit_names_rules_that_drift_from_the_shipped_file(monkeypatch):
    """Rediģējamā kopija uz servera tiek uzsēta vienu reizi, un `play` bloka
    iekšējās izmaiņas tur nekad nenonāk. Audits to nosauc — citādi kods saka
    vienu, bet sistēma dara citu, un neviens to neredz."""
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    assert play.rule_overrides(config.load_rules()) == {}
    drifted = {"play": {**config.load_rules()["play"], "min_seconds": 300}}
    out = play.rule_overrides(drifted)
    assert out["min_seconds"]["live"] == 300
    assert out["min_seconds"]["code"] == play.settings(config.load_rules())["min_seconds"]
    # veco `allowed_genres` atslēgu uz servera audits nosauc...
    stale = {"play": {**config.load_rules()["play"],
                      "somber": {"allowed_genres": ["komēdija"]}}}
    assert "somber" in play.rule_overrides(stale)
    # ...bet tā vairs neko nebloķē: somber saplūst dziļi ar koda noklusējumu
    assert play.genre_ok_on_somber_day(
        Article(raw_json={"_play": {"genres": ["Romantika"]}}), stale) is True


def test_short_films_pass_but_short_episodes_do_not(monkeypatch):
    """Audits parādīja abas puses: «Suns Funs un Rīga» ir 281 s animēta īsfilma
    (īsts katalogs), bet sitemapa «sērijas» ir arī sporta spēļu apskati 81–180 s.
    Tie nav AVOD saturs un noveco kā ziņa, kamēr katalogs nenoveco vispār."""
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    cfg = play.settings({})
    assert play.excluded({"kind": "movie", "show": "suns-funs", "seconds": 281}, cfg) == ""
    assert play.excluded({"kind": "episode", "show": "basketbols",
                          "seconds": 180}, cfg).startswith("par īsu")
    assert play.excluded({"kind": "episode", "show": "klarksona-ferma",
                          "seconds": 2700}, cfg) == ""


def test_reset_rule_block_replaces_the_seeded_copy_but_keeps_the_switch(tmp_path, monkeypatch):
    """Rediģējamo kopiju uzsēj vienu reizi, tāpēc `play` bloka labojumi tur
    nenonāk paši. Poga tos pieņem — bet ieslēgtu slēdzi neizslēdz."""
    monkeypatch.setattr(config, "RULES_DIR", tmp_path)
    stale = config._yaml_blocks(
        (config.DEFAULT_RULES_DIR / "rules.yaml").read_text(encoding="utf-8"))["play"]
    stale = stale.replace("min_seconds: 120", "min_seconds: 300")
    stale = stale.replace("enabled: false", "enabled: true")
    (tmp_path / "rules.yaml").write_text("quiet_hours: []\n\n" + stale + "\n", encoding="utf-8")
    assert play.rule_overrides()["min_seconds"]["live"] == 300

    assert config.reset_rule_block("play", keep=("enabled",)) is True
    assert play.rule_overrides() == {}
    assert play.settings()["min_seconds"] == 120
    # ...un slēdzis, ko redaktors ieslēdza, paliek ieslēgts
    assert play.settings()["enabled"] is True
    # pārējie noteikumi failā netiek aiztikti
    assert "quiet_hours: []" in (tmp_path / "rules.yaml").read_text(encoding="utf-8")
