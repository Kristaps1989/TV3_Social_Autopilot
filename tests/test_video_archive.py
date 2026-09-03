"""tv3.lv/video arhīvs: klipa lapa, saraksts, raksta piesaiste, pārlūkošana,
formāti tikai reel/story, saite uz konkrēto video lapu, dienas limits."""
from datetime import datetime, timedelta
from pathlib import Path

from app import config, pagemeta, pipeline, slots, videos
from app.models import Article, Post, utcnow

FIX = Path(__file__).parent / "fixtures"
VIDEO_PAGE = (FIX / "video_page.html").read_text(encoding="utf-8")
LISTING = (FIX / "video_listing.html").read_text(encoding="utf-8")
VIDEO_URL = "https://tv3.lv/video/195340749/"
ARTICLE_URL = "https://tv3.lv/sports/teniss/ostapenko-parceltaja-maca-zaude/"

ARTICLE_WITH_VIDEO = """<!doctype html><html><head>
<script type="application/ld+json">
{"@type":"NewsArticle","headline":"Ostapenko zaudē","articleBody":"Teksts.",
 "video":{"@type":"VideoObject","name":"Ostapenko pēc mača",
          "url":"https://tv3.lv/video/195340749/ostapenko-pec-maca/",
          "contentUrl":"https://media.tv3.lv/video/195340749/1080x1920.mp4"}}
</script></head><body>
<section class="tv3-single-content"><p>Pirmo setu Ostapenko paņēma ar 6-4, taču pēc lietus pārtraukuma spēle apgriezās otrādi.</p></section>
<aside class="tv3-sidebar"><a href="/video/111222333/">Sānjoslas klips</a></aside>
</body></html>"""

ARTICLE_SIDEBAR_ONLY = """<html><body>
<section class="tv3-single-content"><p>Raksts bez sava video, tikai sānjosla ar ieteikumiem.</p></section>
<aside class="tv3-sidebar"><a href="/video/111222333/">Sānjoslas klips</a></aside>
</body></html>"""


def test_video_page_parses_clip_thumbnail_duration_and_article():
    info = videos.parse_video_page(VIDEO_PAGE, "https://tv3.lv/video/195340749/ostapenko-pec-maca/")
    assert info["id"] == "195340749"
    assert info["url"] == VIDEO_URL                       # kanoniskā forma bez slug
    assert info["title"] == 'Ostapenko pēc mača: "Lietus man nepalīdzēja"'
    assert info["clip"] == "https://media.tv3.lv/video/195340749/1080x1920.mp4"
    assert info["thumbnail"].startswith("https://cdn.tv3.lv/thumbnails/2600x1462/")  # platākais
    assert info["seconds"] == 48
    assert info["upload_date"].startswith("2026-09-03")
    assert info["tags"] == ["Aļona Ostapenko", "US Open"]
    assert info["categories"] == ["Sports"]
    assert info["article"] == ARTICLE_URL
    assert videos.section_for(info, videos.settings({})) == "sport"
    assert videos.parse_duration("PT1H2M3S") == 3723 and videos.parse_duration(90) == 90


def test_listing_gives_unique_canonical_video_urls_in_order():
    assert videos.parse_listing(LISTING) == [
        "https://tv3.lv/video/195340749/",
        "https://tv3.lv/video/195340748/",
        "https://tv3.lv/video/195340747/",
    ]
    assert videos.canonical_url("/video/195340747/?autoplay=1") == "https://tv3.lv/video/195340747/"
    assert videos.canonical_url("https://tv3.lv/zinas/x/") == ""


def test_article_page_links_its_own_video_but_not_the_sidebar():
    meta = pagemeta.parse(ARTICLE_WITH_VIDEO)
    assert meta["video_page"] == VIDEO_URL
    assert meta["video_clip"].endswith("1080x1920.mp4")
    # sānjoslas ieteikums nav raksta video
    assert pagemeta.parse(ARTICLE_SIDEBAR_ONLY).get("video_page", "") == ""


def test_enrich_attaches_the_clip_and_reels_and_stories_link_to_the_video_page(
        session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    a = Article(guid="va-1", url=ARTICLE_URL, canonical_url=ARTICLE_URL,
                title="Ostapenko zaudē", section="sport",
                images=["https://cdn/o.jpg"], raw_json={})
    session.add(a)
    session.flush()
    monkeypatch.setattr(pagemeta, "fetch", lambda url, timeout=10: ARTICLE_WITH_VIDEO)
    pagemeta.enrich(a)
    assert a.raw_json["_video_page"] == VIDEO_URL
    assert a.raw_json["_video_url"].endswith("1080x1920.mp4")
    from app import reels

    assert reels.article_video(a).endswith(".mp4")
    assert "saite tad ved uz " + VIDEO_URL in pagemeta.video_hint(a)
    # saite: lente un stāsts uz video lapu, saites ieraksts uz rakstu
    assert videos.link_for(a, "reel") == VIDEO_URL
    assert videos.link_for(a, "story") == VIDEO_URL
    assert videos.link_for(a, "link") == ARTICLE_URL
    assert videos.link_for(a, "reel", {"video_archive": {"link_reels_to_video": False}}) == ARTICLE_URL


def test_crawl_creates_items_only_for_clips_no_article_carries(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    covered = Article(guid="va-2", url=ARTICLE_URL, canonical_url=ARTICLE_URL,
                      title="Raksts ar klipu", section="sport",
                      raw_json={"_video_page": "https://tv3.lv/video/195340748/"})
    session.add(covered)
    session.commit()
    pages = {
        "https://tv3.lv/video/": LISTING,
        VIDEO_URL: VIDEO_PAGE,
        # bez klipa adreses — izlaiž
        "https://tv3.lv/video/195340747/": "<html><body><h1>Vētra</h1></body></html>",
    }
    fetched = []

    def fetch(url, timeout=10):
        fetched.append(url)
        return pages.get(url, "")

    out = videos.crawl(session, fetch=fetch)
    assert out == {**out, "seen": 3, "new": 1, "covered": 1, "skipped": 1}
    assert "https://tv3.lv/video/195340748/" not in fetched      # rakstā jau ir
    item = videos.existing_item(session, VIDEO_URL)
    assert item is not None and videos.is_video_item(item)
    assert item.guid == "video:195340749" and item.feed_name == "video_archive"
    assert item.section == "sport" and item.editor_status == "can"
    assert item.url == VIDEO_URL and item.images[0].startswith("https://cdn.tv3.lv/")
    assert item.raw_json["_video_url"].endswith(".mp4")
    assert item.raw_json["_video_seconds"] == 48
    assert item.published_at == datetime(2026, 9, 3, 4, 15)     # 07:15+03:00 -> UTC
    assert videos.last_crawl(session)["new"] == 1
    # otrreiz: nekas jauns, esošo nedublē
    assert videos.crawl(session, fetch=fetch)["new"] == 0

    # kopsavilkums diagnostikai
    summ = videos.summary(session)
    assert summ["items"] == 1 and summ["items_with_clip"] == 1
    assert summ["linked_articles"] == 1


def test_video_items_get_only_reel_or_story_and_link_to_the_video_page(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    from app import formats, reels

    item = videos.upsert_item(session, videos.parse_video_page(VIDEO_PAGE, VIDEO_URL),
                              videos.settings({}))
    assert formats.suitable_formats(item, ["link", "photo", "card_carousel", "reel"]) == ["reel"]
    assert formats.suitable_formats(item, ["story"]) == ["story"]
    assert formats.suitable_formats(item, ["link", "photo"]) == []
    assert videos.channel_formats({"formats": ["link", "photo"]}) == []

    built = {}
    monkeypatch.setattr(reels, "available", lambda: True)
    monkeypatch.setattr(reels, "build_video_reel",
                        lambda url, **k: built.setdefault("url", url) and "data/cards/v.mp4")
    notes: list[str] = []
    trace: dict = {}
    fmt, media, recipe = pipeline.resolve_format(
        session, "fb_video", {"formats": ["link", "photo", "reel"], "platform": "facebook_page"},
        item, {"format": "link"}, notes=notes, trace=trace)
    assert fmt == "reel" and media == ["data/cards/v.mp4"]
    assert built["url"].endswith("1080x1920.mp4")
    assert recipe == {"kind": "video_clip", "video": VIDEO_URL}
    assert trace["decision"].startswith("tv3.lv/video klips")
    # stāstu kanāls: story no klipa
    fmt, media, _ = pipeline.resolve_format(
        session, "fb_stories", {"formats": ["story"], "platform": "facebook_page"}, item, {})
    assert fmt == "story"
    assert videos.link_for(item, "reel") == VIDEO_URL
    assert "arhīva klips (48 s)" in pagemeta.video_hint(item)


def test_daily_cap_freshness_and_priority_treat_clips_as_filler(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    from app.rules_engine import evaluate

    now = utcnow()
    item = videos.upsert_item(session, videos.parse_video_page(VIDEO_PAGE, VIDEO_URL),
                              videos.settings({}))
    item.published_at = now - timedelta(hours=30)    # ziņai par vecu, klipam ne
    rules = config.load_rules()
    v = evaluate(item, "fb_video", {"formats": ["reel"]}, rules, now)
    assert v.outcome == "eligible"
    assert v.fresh_until == item.published_at + timedelta(hours=72)

    # prioritāte: 30 h vecs klips ar pusperiodu 48 h vēl ir ~65 % vērtības
    p = Post(article_id=item.id, channel="fb_video", format="reel", copy="c",
             scheduled_at=now + timedelta(hours=1), state="scheduled", extra={})
    session.add(p)
    session.flush()
    assert 0.6 < slots.priority(p, now) / (1.0 + float(item.ai_score or 0)) < 0.7

    # dienas limits: 3 klipi kanālā dienā
    assert videos.posted_today(session, "fb_video") == 1
    for i in range(2):
        session.add(Post(article_id=item.id, channel="fb_video", format="reel", copy="c",
                         scheduled_at=now + timedelta(hours=2 + i), state="scheduled", extra={}))
    session.flush()
    assert videos.over_daily_cap(session, "fb_video") is True
    assert videos.over_daily_cap(session, "fb_other") is False


def test_probe_route_reports_what_the_parser_sees(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    from fastapi.testclient import TestClient

    from app.main import app

    pages = {"https://tv3.lv/video/": LISTING, VIDEO_URL: VIDEO_PAGE,
             ARTICLE_URL: ARTICLE_WITH_VIDEO}
    monkeypatch.setattr(pagemeta, "fetch", lambda url, timeout=10: pages.get(url, ""))
    client = TestClient(app)
    client.post("/setup", data={"password": "slepens123", "password2": "slepens123"})
    r = client.get("/logs/video-probe", params={"url": "https://tv3.lv/video/"}).json()
    assert r["kind"] == "listing" and len(r["videos"]) == 3
    r = client.get("/logs/video-probe", params={"url": VIDEO_URL}).json()
    assert r["kind"] == "video" and r["video"]["clip"].endswith(".mp4")
    r = client.get("/logs/video-probe", params={"url": ARTICLE_URL}).json()
    assert r["kind"] == "article" and r["video_page"] == VIDEO_URL
    assert client.get("/logs/video-probe", params={"url": "https://cits.lv/"}).status_code == 400
    # Diagnostikas lapa rāda arhīva bloku
    assert "tv3.lv/video arhīvs" in client.get("/logs").text


def test_probe_raw_and_find_expose_a_js_shell_and_its_api(session, monkeypatch):
    """tv3.lv/video izrādījās JavaScript čaula (3 KB, vienāda katram
    maršrutam): klipus dod API. Zonde tad parāda skriptus un meklē adreses."""
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    shell = """<!doctype html><html><head><meta property="og:title" content="TV3 video">
<link rel="stylesheet" href="https://tv3cdn.lv/dist/abc/skaties/app.css">
<script>window.__ENV__={"api":"https://tv3.lv/api/v2/"}</script>
<script src="https://tv3cdn.lv/dist/abc/skaties/app.js"></script></head><body><div id="app"></div></body></html>"""
    bundle = 'fetch(`${e}/api/v2/videos?page=${n}`);u="https://skaties.lv/api/video/"+t+".json";x="/graphql/videos"'
    pages = {"https://tv3.lv/video/": shell, "https://tv3cdn.lv/dist/abc/skaties/app.js": bundle}
    monkeypatch.setattr(pagemeta, "fetch", lambda url, timeout=10: pages.get(url, ""))
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    client.post("/setup", data={"password": "slepens123", "password2": "slepens123"})
    r = client.get("/logs/video-probe", params={"url": "https://tv3.lv/video/", "raw": "1"}).json()
    assert r["videos"] == [] and "JavaScript" in r["hint"]
    assert r["scripts"] == ["https://tv3cdn.lv/dist/abc/skaties/app.js"]
    assert r["globals"] == ["__ENV__"]
    assert "https://tv3.lv/api/v2/" in r["found"]
    assert r["html"].startswith("<!doctype")
    r = client.get("/logs/video-probe",
                   params={"url": "https://tv3cdn.lv/dist/abc/skaties/app.js", "find": "api"}).json()
    assert r["kind"] == "text"
    assert any(h.startswith("/api/v2/videos?page=") for h in r["found"])
    assert "https://skaties.lv/api/video/" in r["found"]
    assert "/graphql/videos" in r["found"]
    assert client.get("/logs/video-probe", params={"url": "https://evil.example/"}).status_code == 400


def test_auto_investigation_walks_shell_scripts_and_reports_api_candidates(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    shell = """<!doctype html><html><head>
<script src="/dist/abc/skaties/vendor.js"></script>
<script src="https://tv3cdn.lv/dist/abc/skaties/app.js"></script>
<script src="https://evil.example/x.js"></script></head><body></body></html>"""
    pages = {"https://tv3.lv/video/": shell,
             "https://tv3.lv/dist/abc/skaties/vendor.js": "var a=1;",
             "https://tv3cdn.lv/dist/abc/skaties/app.js":
                 'fetch("https://api.skaties.lv/v1/videos?limit=20");src="https://cdn.x/clip/1.m3u8";r="/video/"+id'}
    monkeypatch.setattr(pagemeta, "fetch", lambda url, timeout=10: pages.get(url, ""))
    out = videos.investigate()
    assert out["shell"]["videos"] == []
    assert [b["script"] for b in out["bundles"]] == [
        "https://tv3.lv/dist/abc/skaties/vendor.js", "https://tv3cdn.lv/dist/abc/skaties/app.js"]
    app_bundle = out["bundles"][1]
    assert "https://api.skaties.lv/v1/videos?limit=20" in app_bundle["api"]
    assert "https://cdn.x/clip/1.m3u8" in app_bundle["clips"]
    assert any(s.startswith("pārbaudīti 2 skripti") for s in out["steps"])

    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    client.post("/setup", data={"password": "slepens123", "password2": "slepens123"})
    r = client.get("/logs/video-probe/auto").json()
    assert r["bundles"][1]["api"]
    page = client.get("/logs").text
    assert "Izpētīt tv3.lv/video automātiski" in page and "built-in method" not in page


def test_crawl_prefers_the_clip_api_over_the_js_shell(session, monkeypatch):
    """Portāla /video ir JS čaula; klipu saraksts nāk no
    https://tv3.lv/api/1/video/feed/. Lauku nosaukumi ir minēti toleranti."""
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    feed = {"items": [
        {"id": 196154563, "title": "Ostapenko pēc mača", "description": "Komentārs",
         "image": {"url": "https://tv3cdn.lv/thumb/1.jpg"}, "duration": 48,
         "published_at": "2026-09-03T07:15:00+03:00",
         "categories": [{"name": "Sports"}], "tags": ["US Open"],
         "streams": {"hls": "https://media.tv3.lv/196154563/index.m3u8"}},
        {"id": 196154562, "title": "Bez straumes feed'ā", "image": "https://tv3cdn.lv/thumb/2.jpg",
         "duration": "PT1M"},
        {"id": 196154561, "title": "Par garu", "duration": 900,
         "video_url": "https://media.tv3.lv/196154561.mp4"},
    ]}
    import json as _json

    calls = []

    def fetch(url, timeout=10):
        calls.append(url)
        if url == "https://tv3.lv/api/1/video/feed/":
            return _json.dumps(feed)
        if url == "https://player.example/tv3/video/196154562":
            return _json.dumps({"data": {"sources": [{"type": "hls",
                                                       "src": "https://media.tv3.lv/196154562/index.m3u8"}]}})
        return "<!doctype html><html><body><div id=root></div></body></html>"

    rules = {**config.load_rules(),
             "video_archive": {"player_api": "https://player.example/tv3/video/{id}"}}
    out = videos.crawl(session, rules=rules, fetch=fetch)
    assert out["source"] == "api" and out["seen"] == 3
    assert out["new"] == 2 and out["skipped"] == 1          # 900 s ir par garu
    a = videos.existing_item(session, "https://tv3.lv/video/196154563/")
    assert a.title == "Ostapenko pēc mača" and a.section == "sport"
    assert a.raw_json["_video_url"].endswith("index.m3u8") and a.raw_json["_video_seconds"] == 48
    assert a.images == ["https://tv3cdn.lv/thumb/1.jpg"]
    b = videos.existing_item(session, "https://tv3.lv/video/196154562/")
    assert b.raw_json["_video_url"] == "https://media.tv3.lv/196154562/index.m3u8"   # no atskaņotāja API
    assert not any(u.startswith("https://tv3.lv/video/") for u in calls)          # čaulu nelasa


def test_investigation_samples_portal_apis_and_lists_url_constants(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    shell = '<!doctype html><html><head><script type="module" src="/video/assets/index-X.js"></script></head><body></body></html>'
    bundle = ('const LA="https://player.skaties.lv/api";fetch("https://tv3.lv/api/1/video/feed/");'
              'u=`${LA}/tv3/video/${e}`;g="https://tv3.lv/api/1/video/menu/"')
    pages = {"https://tv3.lv/video/": shell,
             "https://tv3.lv/video/assets/index-X.js": bundle,
             "https://tv3.lv/api/1/video/feed/": '{"items":[{"id":1,"title":"t","streams":{"hls":"x.m3u8"}}]}',
             "https://tv3.lv/api/1/video/menu/": '[{"id":3,"name":"Sports"}]'}
    monkeypatch.setattr(pagemeta, "fetch", lambda url, timeout=10: pages.get(url, ""))
    out = videos.investigate()
    urls = [s["url"] for s in out["api_samples"]]
    assert "https://tv3.lv/api/1/video/feed/" in urls and "https://tv3.lv/api/1/video/menu/" in urls
    feed_sample = next(s for s in out["api_samples"] if s["url"].endswith("/feed/"))
    assert feed_sample["json_shape"]["items"][0] == "list[1]"
    assert "LA = https://player.skaties.lv/api" in out["url_constants"]
    assert any("/tv3/video/" in c for c in out["context"])


def test_real_feed_schema_links_articles_and_creates_items_for_the_rest(session, monkeypatch):
    """Īstais https://tv3.lv/api/1/video/feed/ paraugs: video_url ir HLS,
    duration_ms, related_url norāda rakstu, content_source avotu."""
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    feed = (FIX / "video_feed.json").read_text(encoding="utf-8")
    # raksts, kas mums jau ir (ar www un bez noslēdzošā / — jānormalizē)
    nepal = Article(guid="np-1", url="https://www.tv3.lv/zinas/arvalstis/glabeji-cinas-ar-laiku-un-nogruvumiem-katastrofas-apmers-nepala-klust-arvien-tragiskaks",
                    canonical_url="https://www.tv3.lv/zinas/arvalstis/glabeji-cinas-ar-laiku-un-nogruvumiem-katastrofas-apmers-nepala-klust-arvien-tragiskaks",
                    title="Nepāla", section="news", images=["https://cdn/n.jpg"], raw_json={})
    session.add(nepal)
    session.commit()
    calls = []

    def fetch(url, timeout=10):
        calls.append(url)
        if url == "https://tv3.lv/api/1/video/feed/":
            return feed
        if url == "https://tv3.lv/api/1/video/feed/?page=2":
            return '{"meta":{"has_more":false},"items":[]}'
        return ""

    out = videos.crawl(session, fetch=fetch)
    assert out["source"] == "api" and out["seen"] == 4
    assert "https://tv3.lv/api/1/video/feed/?page=2" in calls          # lapo, kamēr has_more
    # raksts saņem savu klipu no feed puses
    assert nepal.raw_json["_video_page"] == "https://tv3.lv/video/196425621/"
    assert nepal.raw_json["_video_url"].endswith("70274191.m3u8")
    assert nepal.raw_json["_video_seconds"] == 32
    assert out["linked"] == 1 and out["covered"] == 1
    assert videos.link_for(nepal, "reel") == "https://tv3.lv/video/196425621/"
    # pārējie trīs kļūst par rindām ar pareizu sadaļu
    assert out["new"] == 3
    bt = videos.existing_item(session, "https://tv3.lv/video/196776462/")
    assert bt.section == "entertainment" and bt.raw_json["_video_source"] == "Bez Tabu"
    assert bt.raw_json["_video_share_image"].endswith("share.jpg")
    assert bt.images == ["https://tv3cdn.lv/video/thumb/196776462/70592056/poster.jpg"]
    rtu = videos.existing_item(session, "https://tv3.lv/video/193066443/")
    assert rtu.section == "news"                                       # /zinas/ raksta ceļš
    assert rtu.raw_json["_video_article"].startswith("https://tv3.lv/zinas/latvija/")
    smiltene = videos.existing_item(session, "https://tv3.lv/video/184688698/")
    assert smiltene.section == "entertainment" and smiltene.raw_json["_video_seconds"] == 48
    assert smiltene.published_at == datetime(2026, 8, 10, 8, 8, 4)

    # raksts ienāk vēlāk: nākamais apgājiens to piesaista un pārņem atsevišķo rindu
    art = Article(guid="rtu-1", url="https://tv3.lv/zinas/latvija/rtu-studenti-uzbuvejusi-formulu-un-startes-starptautiskas-sacensibas/",
                  canonical_url="https://tv3.lv/zinas/latvija/rtu-studenti-uzbuvejusi-formulu-un-startes-starptautiskas-sacensibas/",
                  title="RTU formula", section="news", raw_json={})
    session.add(art)
    session.commit()
    videos.crawl(session, fetch=fetch)
    assert art.raw_json["_video_page"] == "https://tv3.lv/video/193066443/"
    assert rtu.decided_at is not None and rtu.raw_json["_video_superseded"] == art.id


def test_play_investigation_reads_inline_data_sitemap_and_apis(session, monkeypatch):
    """play.tv3.lv izpēte: tā pati ķēde, plus Next dati, sitemap un robots."""
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    shell = """<!doctype html><html><head>
<link rel="alternate" type="application/rss+xml" href="https://play.tv3.lv/feed.xml">
<script type="application/ld+json">{"@type":"WebSite","name":"TV3 Play"}</script>
<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":{"titles":[{"id":7,"title":"Seriāls"}]}}}</script>
<script src="/_next/static/chunks/main-abc.js"></script></head>
<body><a href="/seriali/x/">S</a><a href="/seriali/y/">S2</a><a href="/filmas/z/">F</a>
<a href="https://tv3.lv/zinas/">ārējs</a></body></html>"""
    pages = {
        "https://play.tv3.lv/": shell,
        "https://play.tv3.lv/_next/static/chunks/main-abc.js":
            'fetch("https://play.tv3.lv/api/v1/catalog?type=movie");x="https://api.play.tv3.lv/graphql"',
        "https://play.tv3.lv/robots.txt": "User-agent: *\nSitemap: https://play.tv3.lv/sitemap.xml\n",
        "https://play.tv3.lv/sitemap.xml": "<urlset><url><loc>https://play.tv3.lv/filmas/z/</loc></url></urlset>",
        "https://play.tv3.lv/api/v1/catalog?type=movie": '{"items":[{"id":1,"title":"Filma"}]}',
    }
    monkeypatch.setattr(pagemeta, "fetch", lambda url, timeout=10: pages.get(url, ""))
    out = videos.investigate(site="play")
    assert out["listing"] == "https://play.tv3.lv/"
    assert out["content"]["inline_data"][0]["name"] == "__NEXT_DATA__"
    assert out["content"]["json_ld_types"] == ["WebSite"]
    assert out["content"]["internal_paths"][0] == {"prefix": "/seriali/", "count": 2,
                                                   "example": "https://play.tv3.lv/seriali/x/"}
    assert any("rss+xml" in a for a in out["content"]["alternate_links"])
    robots = next(f for f in out["site_files"] if f["url"].endswith("robots.txt"))
    assert robots["sitemaps"] == ["https://play.tv3.lv/sitemap.xml"]
    sitemap = next(f for f in out["site_files"] if f["url"].endswith("/sitemap.xml"))
    assert sitemap["urls"] == ["https://play.tv3.lv/filmas/z/"]
    assert "https://play.tv3.lv/api/v1/catalog?type=movie" in out["bundles"][0]["api"]
    sample = next(s for s in out["api_samples"] if "catalog" in s["url"])
    assert sample["json_shape"]["items"][0] == "list[1]"

    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    client.post("/setup", data={"password": "slepens123", "password2": "slepens123"})
    assert client.get("/logs/site-probe/auto", params={"site": "play"}).json()["site"] == "play"
    assert client.get("/logs/site-probe/auto", params={"site": "x"}).status_code == 400
    assert "Izpētīt TV3 Play automātiski" in client.get("/logs").text
