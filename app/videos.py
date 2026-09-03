"""tv3.lv/video arhīvs: portāla vertikālo klipu (iekšējā «TikTok») izmantošana.

Divi ceļi, kā klips nonāk sociālajos tīklos, un abos saite ved uz konkrēto
video lapu `tv3.lv/video/<id>/`, ne uz rakstu vai sākumlapu:

1. **Raksts ar piesaistītu video.** Raksta lapa (JSON-LD `video`, og:video vai
   saite `/video/<id>/` satura blokā) pasaka, kurš klips pie raksta pieder.
   Tad reel un story top no ĪSTĀ klipa, ne slideshow, un ieraksta saite ved
   uz video lapu (`link_reels_to_video`).
2. **Klips bez raksta.** Arhīva saraksts (`tv3.lv/video/` vai feed) dod
   klipus, kurus neviens raksts nenes. Tie kļūst par pašu rakstu rindām ar
   `raw_json["_video"]`, iet cauri tam pašam AI lēmumam, bet formāts ir tikai
   reel vai story, un tie NEKONKURĒ ar ziņām: garš pusperiods, savs dienas
   limits, rindā aizpilda tukšumus.

Portāla lapas struktūra nav apstiprināta no būves vides (tīkls slēgts),
tāpēc parsētājs lasa vairākus signālus (schema.org VideoObject, og:video,
<video>/<source>, mp4/m3u8 adreses, dlEvent) un lapā Diagnostika ir zonde
(`/logs/video-probe`), ar ko to pārbaudīt pret dzīvo portālu.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import select

from app import config, pagemeta
from app.models import Article, Post, get_setting, set_setting, utcnow

log = logging.getLogger("autopilot.videos")

VIDEO_HOST = "https://tv3.lv"
FEED_NAME = "video_archive"
VIDEO_FORMATS = ("reel", "story")

# tv3.lv/video/195340749/ (ar vai bez slug aiz id, relatīvs vai pilns)
VIDEO_URL_RE = re.compile(
    r"(?:https?://(?:www\.)?tv3\.lv)?/video/(?P<id>\d{3,})(?:/[^\s\"'<>?#]*)?/?",
    re.I)
_CLIP_RE = re.compile(r"https?://[^\s\"'<>]+?\.(?:mp4|m3u8)(?:\?[^\s\"'<>]*)?", re.I)
_VIDEO_TAG_RE = re.compile(r"<(?:video|source)\b[^>]*\bsrc=[\"']([^\"']+)[\"']", re.I)
_DURATION_RE = re.compile(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?$")

DEFAULTS = {
    "enabled": True,
    "listing": "https://tv3.lv/video/",
    "feed": "",
    "interval_minutes": 30,
    "max_new_per_run": 6,
    "min_seconds": 5,
    "max_seconds": 180,
    "daily_cap": 3,
    "half_life_hours": 48,
    "max_age_hours": 72,
    "link_reels_to_video": True,
    "section_default": "entertainment",
    "category_sections": {
        "Ziņas": "news", "Latvija": "news", "Pasaule": "news", "Sabiedrība": "news",
        "Sports": "sport", "Izklaide": "entertainment", "Dzīve": "entertainment",
        "Šovi": "entertainment", "Seriāli": "entertainment", "Kino": "entertainment",
    },
}


def settings(rules: dict | None = None) -> dict:
    rules = config.load_rules() if rules is None else rules
    cfg = rules.get("video_archive")
    if cfg is None:
        return dict(DEFAULTS)
    if cfg is False:
        return {**DEFAULTS, "enabled": False}
    merged = {**DEFAULTS, **(cfg if isinstance(cfg, dict) else {})}
    merged["category_sections"] = {**DEFAULTS["category_sections"],
                                   **((cfg or {}).get("category_sections") or {})}
    return merged


# --- URL palīgi --------------------------------------------------------------

def video_id(url: str) -> str:
    m = VIDEO_URL_RE.search(url or "")
    return m.group("id") if m else ""


def canonical_url(url_or_id: str) -> str:
    vid = video_id(url_or_id) or (url_or_id if str(url_or_id).isdigit() else "")
    return f"{VIDEO_HOST}/video/{vid}/" if vid else ""


def is_video_url(url: str) -> bool:
    return bool(video_id(url))


def video_links(html: str) -> list[str]:
    """Visas video lapu saites HTML, unikālas, dokumenta secībā."""
    out: list[str] = []
    for m in VIDEO_URL_RE.finditer(html or ""):
        u = canonical_url(m.group(0))
        if u and u not in out:
            out.append(u)
    return out


def clip_urls(html: str) -> list[str]:
    """mp4/m3u8 adreses lapā (<video>/<source> pirmie, tad viss pārējais)."""
    out: list[str] = []
    for m in _VIDEO_TAG_RE.finditer(html or ""):
        u = m.group(1).replace("\\/", "/")
        if u.startswith("http") and u not in out:
            out.append(u)
    for m in _CLIP_RE.finditer((html or "").replace("\\/", "/")):
        if m.group(0) not in out:
            out.append(m.group(0))
    return out


def parse_duration(value) -> int:
    """ISO 8601 (PT1M30S) vai skaitlis sekundēs -> sekundes (0, ja nav)."""
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip().upper()
    if text.isdigit():
        return int(text)
    m = _DURATION_RE.match(text)
    if not m:
        return 0
    days, hours, minutes, seconds = m.groups()
    return (int(days or 0) * 86400 + int(hours or 0) * 3600
            + int(minutes or 0) * 60 + int(float(seconds or 0)))


def _first_str(value) -> str:
    if isinstance(value, list):
        for v in value:
            s = _first_str(v)
            if s:
                return s
        return ""
    if isinstance(value, dict):
        return _first_str(value.get("url") or value.get("contentUrl")
                          or value.get("@id") or "")
    return str(value or "").strip()


def _video_objects(html: str) -> list[dict]:
    """schema.org VideoObject mezgli: gan patstāvīgi, gan raksta `video` laukā."""
    out = []
    for node in pagemeta._json_ld_nodes(html):
        stack = [node]
        while stack:
            cur = stack.pop()
            if not isinstance(cur, dict):
                continue
            types = cur.get("@type")
            types = types if isinstance(types, list) else [types]
            if "VideoObject" in types:
                out.append(cur)
            for key in ("video", "@graph", "mainEntity", "subjectOf"):
                child = cur.get(key)
                if isinstance(child, list):
                    stack.extend(c for c in child if isinstance(c, dict))
                elif isinstance(child, dict):
                    stack.append(child)
    return out


# --- video lapa --------------------------------------------------------------

def parse_video_page(html: str, url: str = "") -> dict:
    """Viena video lapa -> {id, url, title, description, thumbnail, clip,
    embed, seconds, upload_date, tags, categories, post_id, article}.

    Klipa adrese (`clip`) ir tas, no kā ffmpeg būvē reel: VideoObject
    contentUrl, tad <video>/<source>, tad jebkura mp4/m3u8 lapā. Bez tās
    klips ir tikai saite, un reel no tā uzbūvēt nevar.
    """
    html = html or ""
    objs = _video_objects(html)
    vo = objs[0] if objs else {}
    canon = (canonical_url(url) or canonical_url(pagemeta._meta_one(html, "og:url"))
             or next(iter(video_links(html)), ""))
    clips = clip_urls(html)
    clip = _first_str(vo.get("contentUrl")) or next(iter(clips), "") or ""
    og_video = pagemeta._meta_one(html, "og:video:secure_url", "og:video:url", "og:video")
    if not clip and og_video and re.search(r"\.(mp4|m3u8)(\?|$)", og_video, re.I):
        clip = og_video
    thumb = (_first_str(vo.get("thumbnailUrl")) or pagemeta._meta_one(html, "og:image")
             or "")
    if thumb:
        thumb = pagemeta._widest_variant(html, thumb)
    meta = pagemeta.parse(html) if html else {}
    title = (_first_str(vo.get("name")) or pagemeta._meta_one(html, "og:title")
             or _first_str(meta.get("title")) or "")
    title = re.sub(r"\s*[|–-]\s*(TV3|tv3\.lv)\s*$", "", title).strip()
    desc = (_first_str(vo.get("description")) or pagemeta._meta_one(html, "og:description")
            or "").strip()
    # raksts, pie kura klips pieder: JSON-LD isPartOf/mainEntityOfPage vai
    # pirmā raksta saite ar zināmu sadaļu (ne /video/ un ne sākumlapa)
    article = ""
    for key in ("isPartOf", "mainEntityOfPage", "url"):
        cand = _first_str(vo.get(key))
        if cand.startswith("http") and "tv3.lv" in cand and not is_video_url(cand):
            from app.ingest import url_section

            if url_section(cand, config.url_sections()):
                article = cand
                break
    if not article:
        from app.ingest import url_section

        for m in re.finditer(r"href=[\"'](https?://(?:www\.)?tv3\.lv/[^\"'#?]+)", html, re.I):
            cand = m.group(1)
            if not is_video_url(cand) and url_section(cand, config.url_sections()):
                article = cand
                break
    return {
        "id": video_id(canon),
        "url": canon,
        "title": title,
        "description": desc,
        "thumbnail": thumb,
        "clip": clip,
        "embed": _first_str(vo.get("embedUrl")) or og_video,
        "seconds": parse_duration(vo.get("duration")),
        "upload_date": _first_str(vo.get("uploadDate")) or meta.get("publish_date", ""),
        "tags": meta.get("tags") or [],
        "categories": meta.get("categories") or [],
        "post_id": meta.get("post_id", ""),
        "article": article,
    }


def parse_listing(html: str) -> list[str]:
    """Arhīva saraksta lapa -> video lapu URL dokumenta secībā (jaunākie pirmie)."""
    return video_links(html)


def section_for(info: dict, cfg: dict) -> str:
    table = cfg.get("category_sections") or {}
    for cat in info.get("categories") or []:
        if cat in table:
            return table[cat]
    for tag in info.get("tags") or []:
        if tag in table:
            return table[tag]
    return str(cfg.get("section_default") or "entertainment")


# --- raksta rindas ------------------------------------------------------------

def is_video_item(article) -> bool:
    return bool(article is not None and (article.raw_json or {}).get("_video"))


def video_page(article) -> str:
    """Video lapas URL rakstam ('' ja raksts video nenes)."""
    raw = (article.raw_json or {}) if article is not None else {}
    return str(raw.get("_video_page") or "")


def link_for(article, fmt: str, rules: dict | None = None) -> str:
    """Kur ved ieraksta saite: arhīva klipam vienmēr uz video lapu; raksta
    lentei/stāstam no īstā klipa — uz video lapu (`link_reels_to_video`),
    citādi uz rakstu."""
    page = video_page(article)
    if not page:
        return article.canonical_url or article.url
    if is_video_item(article):
        return page
    if fmt in VIDEO_FORMATS and settings(rules).get("link_reels_to_video", True):
        return page
    return article.canonical_url or article.url


def channel_formats(cfg: dict) -> list[str]:
    """Kuros formātos kanāls var nest arhīva klipu (reel vai story)."""
    return [f for f in (cfg.get("formats") or []) if f in VIDEO_FORMATS]


def _parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def covering_article(session, page_url: str):
    """Raksts, kas šo video jau nes (tad atsevišķa arhīva rinda nav vajadzīga)."""
    if not page_url:
        return None
    rows = session.execute(
        select(Article).where(Article.raw_json["_video_page"].as_string() == page_url)
    ).scalars().all()
    for a in rows:
        if not is_video_item(a):
            return a
    return None


def existing_item(session, page_url: str):
    vid = video_id(page_url)
    if not vid:
        return None
    return session.execute(
        select(Article).where(Article.guid == f"video:{vid}")).scalar_one_or_none()


def upsert_item(session, info: dict, cfg: dict) -> Article | None:
    """Arhīva klips -> raksta rinda ({} ja bez klipa adreses vai ārpus garuma)."""
    if not info.get("url") or not info.get("id"):
        return None
    if not info.get("clip"):
        log.info("video %s: nav klipa adreses lapā — izlaižam", info.get("url"))
        return None
    secs = int(info.get("seconds") or 0)
    if secs and (secs < int(cfg.get("min_seconds", 5))
                 or secs > int(cfg.get("max_seconds", 180))):
        log.info("video %s: %d s ārpus %s–%s s — izlaižam", info["url"], secs,
                 cfg.get("min_seconds"), cfg.get("max_seconds"))
        return None
    row = existing_item(session, info["url"])
    if row is not None:
        return row
    page_meta = {"post_id": info.get("post_id", ""), "tags": info.get("tags") or [],
                 "categories": info.get("categories") or [],
                 "post_types": ["video"], "publish_date": info.get("upload_date", "")}
    row = Article(
        guid=f"video:{info['id']}", url=info["url"], canonical_url=info["url"],
        title=info.get("title") or f"tv3.lv video {info['id']}",
        lead=info.get("description") or "",
        section=section_for(info, cfg),
        categories=list(info.get("categories") or []),
        images=[info["thumbnail"]] if info.get("thumbnail") else [],
        published_at=_parse_date(info.get("upload_date")) or utcnow(),
        editor_status="can", feed_name=FEED_NAME,
        raw_json={"_video": True, "_video_id": info["id"], "_video_url": info["clip"],
                  "_video_page": info["url"], "_video_seconds": secs,
                  "_video_article": info.get("article", ""),
                  "_section_src": "video", "_page_meta": page_meta,
                  "_page_meta_at": utcnow().isoformat(timespec="seconds")},
    )
    session.add(row)
    session.flush()
    return row


def attach_to_article(article, found: dict, fetch=None) -> bool:
    """Raksta lapā atrasts video -> piesaista klipu rakstam.

    `found` ir pagemeta.parse rezultāts (video_page / video_clip). Ja lapa
    devusi tikai video lapas saiti, klipa adresi paņem no pašas video lapas
    (viena papildu ielase — tikai rakstiem, kam video tiešām ir).
    """
    page = canonical_url(found.get("video_page") or "")
    clip = found.get("video_clip") or ""
    if not page and not clip:
        return False
    raw = dict(article.raw_json or {})
    changed = False
    if page and raw.get("_video_page") != page:
        raw["_video_page"] = page
        changed = True
    if not raw.get("_video_url"):
        if not clip and page:
            fetch = fetch or pagemeta.fetch
            html = fetch(page)
            clip = parse_video_page(html, page).get("clip", "") if html else ""
        if clip:
            raw["_video_url"] = clip
            changed = True
    if changed:
        article.raw_json = raw
        log.info("raksts %s: piesaistīts tv3.lv video %s%s", article.id, page or "",
                 " (klips)" if raw.get("_video_url") else " (bez klipa adreses)")
    return changed


# --- pārlūkošana ---------------------------------------------------------------

def _listing_urls(cfg: dict, fetch) -> list[str]:
    urls: list[str] = []
    feed = str(cfg.get("feed") or "")
    if feed:
        body = fetch(feed)
        if body:
            from app.ingest import parse_feed_body

            try:
                items = parse_feed_body(body, "", FEED_NAME, cfg.get("section_default", ""),
                                        {})
            except Exception as e:  # noqa: BLE001
                log.warning("video feed %s nav nolasāms: %s", feed, e)
                items = []
            for it in items:
                u = canonical_url(it.get("url") or it.get("canonical_url") or "")
                if u and u not in urls:
                    urls.append(u)
    listing = str(cfg.get("listing") or "")
    if listing:
        html = fetch(listing)
        for u in parse_listing(html or ""):
            if u not in urls:
                urls.append(u)
    return urls


def crawl(session, rules: dict | None = None, fetch=None, now: datetime | None = None) -> dict:
    """Viens arhīva apgājiens: jaunie klipi kļūst par rindām. Kopsavilkums
    paliek iestatījumos (`video_archive:last`) Diagnostikas lapai."""
    cfg = settings(rules)
    summary = {"enabled": bool(cfg.get("enabled")), "seen": 0, "new": 0,
               "covered": 0, "skipped": 0, "at": (now or utcnow()).isoformat(timespec="seconds")}
    if not cfg.get("enabled"):
        return summary
    fetch = fetch or pagemeta.fetch
    urls = _listing_urls(cfg, fetch)
    summary["seen"] = len(urls)
    budget = int(cfg.get("max_new_per_run") or 6)
    for url in urls:
        if budget <= 0:
            break
        if existing_item(session, url) is not None:
            continue
        if covering_article(session, url) is not None:
            summary["covered"] += 1
            continue
        html = fetch(url)
        if not html:
            summary["skipped"] += 1
            continue
        info = parse_video_page(html, url)
        row = upsert_item(session, info, cfg)
        if row is None:
            summary["skipped"] += 1
            continue
        summary["new"] += 1
        budget -= 1
        log.info("video arhīvs: jauns klips %s «%s» (%s s, %s)", info["id"],
                 row.title[:60], info.get("seconds") or "?", row.section)
    session.commit()
    set_setting(session, "video_archive:last", json.dumps(summary, ensure_ascii=False))
    return summary


def last_crawl(session) -> dict:
    raw = get_setting(session, "video_archive:last", "")
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


# --- kvotas un diagnostika -----------------------------------------------------

def posted_today(session, channel: str, now: datetime | None = None) -> int:
    """Cik arhīva klipu kanālā šodien (Rīgas diena) jau ieplānoti/publicēti."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(config.TIMEZONE)
    now = now or utcnow()
    today = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).date()
    rows = session.execute(
        select(Post).where(Post.channel == channel,
                           Post.state.in_(("scheduled", "publishing", "published")))
    ).scalars().all()
    count = 0
    for p in rows:
        if not is_video_item(p.article):
            continue
        when = p.scheduled_at or p.published_at or p.created_at
        if when and when.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).date() == today:
            count += 1
    return count


def over_daily_cap(session, channel: str, rules: dict | None = None) -> bool:
    cap = int(settings(rules).get("daily_cap") or 0)
    return bool(cap) and posted_today(session, channel) >= cap


def summary(session, rules: dict | None = None) -> dict:
    cfg = settings(rules)
    items = session.execute(
        select(Article).where(Article.feed_name == FEED_NAME)).scalars().all()
    linked = session.execute(
        select(Article).where(Article.raw_json["_video_page"].as_string() != "",
                              Article.feed_name != FEED_NAME)).scalars().all()
    since = utcnow() - timedelta(days=7)
    posts = session.execute(
        select(Post).where(Post.state == "published", Post.published_at >= since)
    ).scalars().all()
    video_posts = [p for p in posts if video_page(p.article)]
    return {
        "enabled": bool(cfg.get("enabled")),
        "listing": cfg.get("listing"), "feed": cfg.get("feed") or "",
        "items": len(items),
        "items_with_clip": sum(1 for a in items if (a.raw_json or {}).get("_video_url")),
        "items_undecided": sum(1 for a in items if a.decided_at is None),
        "linked_articles": len(linked),
        "published_7d": len(video_posts),
        "published_7d_by_format": {
            f: sum(1 for p in video_posts if p.format == f) for f in VIDEO_FORMATS},
        "daily_cap": cfg.get("daily_cap"),
        "last_crawl": last_crawl(session),
    }


def probe(url: str, fetch=None) -> dict:
    """Diagnostikas zonde: ko parsētājs redz dzīvajā lapā (saraksts, video
    lapa vai raksts) — lai portāla struktūru var pārbaudīt bez konsoles."""
    fetch = fetch or pagemeta.fetch
    html = fetch(url)
    out: dict = {"url": url, "fetched": bool(html), "bytes": len(html or "")}
    if not html:
        out["error"] = "lapa neatbild vai nav 200"
        return out
    if is_video_url(url):
        out["kind"] = "video"
        out["video"] = parse_video_page(html, url)
        out["clip_candidates"] = clip_urls(html)[:10]
    elif url.rstrip("/").endswith("/video") or "/video?" in url:
        out["kind"] = "listing"
        out["videos"] = parse_listing(html)[:40]
    else:
        out["kind"] = "article"
        meta = pagemeta.parse(html)
        out["video_page"] = meta.get("video_page", "")
        out["video_clip"] = meta.get("video_clip", "")
        out["video_links_anywhere"] = video_links(html)[:10]
        out["clip_candidates"] = clip_urls(html)[:10]
    return out
