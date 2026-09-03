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
