"""TV3 Play (play.tv3.lv) sociālajos tīklos — P1 pēc docs/play-strategy.md.

Katalogs nāk no Play sitemapiem (Google video paplašinājums: nosaukums,
apraksts, sīktēls, ilgums, datums, lapas adrese), ne no API. Nosaukumi kļūst
par rakstu rindām ar `raw_json["_play"]`, iet cauri tam pašam AI lēmumam, bet:

- formāts tikai link / photo / story (klipu straumes nav — tās ir Go3 DRM);
- saite ved uz konkrēto Play lapu ar `utm_campaign=play`;
- **ētikas sargi kodā**: nekad blakus traģēdijai vai noziegumam (attālums
  plūsmā), drūmas dienas režīms (tikai mierīgi žanri vai pauze), vakara
  logs, vecuma cenzs tikai vēlu vakarā, viena nosaukuma atkārtojums ne biežāk
  kā reizi 14 dienās, un ne vairāk kā desmitā daļa plūsmas;
- Play pēc noklusējuma ir IZSLĒGTS (`play.enabled`) — ieslēdz redaktors.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app import config, pagemeta
from app.models import Article, Post, get_setting, set_setting, utcnow

log = logging.getLogger("autopilot.play")

FEED_NAME = "play"
PLAY_HOST = "https://play.tv3.lv"
PLAY_FORMATS = ("link", "photo", "story")
GRIM_SENSITIVITY = ("tragedy", "crime")

DEFAULTS = {
    "enabled": False,
    "base": PLAY_HOST + "/",
    # sitemapi: jaunākais + tekošais mēnesis ({month} = YYYY-MM)
    "sitemaps": ["/sitemaps/sitemap-latest.xml", "/sitemaps/sitemap-{month}.xml"],
    "interval_minutes": 60,
    "max_new_per_run": 20,
    "page_fetch_per_run": 6,          # nosaukumu lapas žanram/cenzam
    # raidījumi, kas ir ziņas, ne izklaide — Play promo tos neņem
    "exclude_slugs": ["tv3-zinas", "tv3-zinas-isuma", "degpunkta", "900-sekundes",
                      "bez-tabu", "neka-personiga", "tiesraides", "video-1"],
    "sport_slugs": ["fiba", "fifa", "wrc", "hokej", "basketbol", "futbol", "sport"],
    "min_seconds": 300,               # īsāks par 5 min ir sižets, ne saturs
    "daily_cap": 1,                   # darbdienā uz kanālu
    "weekend_daily_cap": 2,
    "story_daily_cap": 1,
    "feed_share": 0.10,               # Play daļa kanāla plūsmā (7 dienas)
    "windows": ["19:00-22:30"],       # vakara logs Rīgā
    "adult_window": "21:00-23:59",    # 16+/18+ tikai vēlu
    "adjacency_minutes": 90,          # attālums no traģēdijas/nozieguma ieraksta
    "somber": {"window_hours": 6, "threshold": 0.4,
               "allowed_genres": ["ģimenes", "komēdija", "drāma", "dokumentāl", "kulinār",
                                  "ceļojum", "daba", "bērn"]},
    "title_cooldown_days": 14,
    "half_life_hours": 72,
    "max_age_hours": 240,
    "genre_overrides": {},            # slug -> [žanri]
    "campaign": "play",
}

_LOC_RE = re.compile(
    r"^https?://(?:www\.)?play\.tv3\.lv/(?P<kind>filmas|video|seriali|sovi-un-raidijumi)/"
    r"(?P<show>[^/]+?)-(?P<show_id>\d+)/(?:(?P<ep>[^/]+?)-(?P<ep_id>\d+)/)?$")
_ADULT_RE = re.compile(r"\b(1[68])\s*\+|\bN-?(1[68])\b|\bK-?(1[68])\b", re.I)


def settings(rules: dict | None = None) -> dict:
    rules = config.load_rules() if rules is None else rules
    cfg = rules.get("play")
    if cfg is None:
        return dict(DEFAULTS)
    if cfg is False:
        return {**DEFAULTS, "enabled": False}
    merged = {**DEFAULTS, **(cfg if isinstance(cfg, dict) else {})}
    merged["somber"] = {**DEFAULTS["somber"], **((cfg or {}).get("somber") or {})}
    return merged


# --- katalogs no sitemapiem ----------------------------------------------------

def parse_loc(url: str) -> dict:
    """Play lapas adrese -> {kind, show, show_id, ep, ep_id, id}."""
    m = _LOC_RE.match((url or "").strip())
    if not m:
        return {}
    kind = m.group("kind")
    ep_id = m.group("ep_id")
    if kind == "filmas":
        typ = "movie"
    elif ep_id:
        typ = "episode"
    else:
        typ = "show"
    return {"kind": typ, "show": m.group("show"), "show_id": m.group("show_id"),
            "ep": m.group("ep") or "", "ep_id": ep_id or "",
            "id": ep_id or m.group("show_id")}


def sitemap_urls(cfg: dict, now: datetime | None = None) -> list[str]:
    now = now or utcnow()
    month = now.strftime("%Y-%m")
    base = str(cfg.get("base") or PLAY_HOST + "/").rstrip("/")
    out = []
    for path in cfg.get("sitemaps") or []:
        url = path.replace("{month}", month)
        url = url if url.startswith("http") else base + url
        if url not in out:
            out.append(url)
    return out


def catalog(fetch=None, cfg: dict | None = None, now: datetime | None = None) -> list[dict]:
    """Sitemap video ieraksti -> kataloga vienības (jaunākie pirmie, unikāli)."""
    from app import videos

    cfg = cfg or settings()
    fetch = fetch or pagemeta.fetch
    seen: set[str] = set()
    items: list[dict] = []
    for url in sitemap_urls(cfg, now):
        body = fetch(url)
        if not body:
            continue
        for entry in videos.sitemap_video_entries(body):
            loc = parse_loc(entry.get("loc", ""))
            if not loc or loc["id"] in seen:
                continue
            seen.add(loc["id"])
            items.append({
                **loc, "url": entry["loc"],
                "title": entry.get("title", "").strip(),
                "description": entry.get("description", "").strip(),
                "thumbnail": entry.get("thumbnail_loc", ""),
                "seconds": int(entry.get("duration") or 0) if str(entry.get("duration", "")).isdigit() else 0,
                "published": entry.get("publication_date") or entry.get("lastmod") or "",
                "player": entry.get("player_loc", ""),
                "tags": entry.get("tags") or [],
            })
    return items


def excluded(item: dict, cfg: dict) -> str:
    """Kāpēc vienība nav Play promo materiāls ('' = der)."""
    show = item.get("show", "")
    for slug in cfg.get("exclude_slugs") or []:
        if show.startswith(slug):
            return f"ziņu raidījums ({slug})"
    if item.get("kind") not in ("movie", "show", "episode"):
        return "nav filma/seriāls/raidījums"
    secs = int(item.get("seconds") or 0)
    if secs and secs < int(cfg.get("min_seconds") or 0):
        return f"par īsu ({secs} s) — sižets, ne saturs"
    return ""


def section_for(item: dict, cfg: dict) -> str:
    text = " ".join([item.get("show", ""), item.get("title", ""),
                     " ".join(item.get("genres") or [])]).lower()
    if any(s in text for s in (cfg.get("sport_slugs") or [])):
        return "sport"
    return "entertainment"


def enrich_from_page(item: dict, fetch=None) -> dict:
    """Nosaukuma lapa -> žanri, cenzs, labāks nosaukums/plakāts (ja lapa tos dod)."""
    from app import videos

    fetch = fetch or pagemeta.fetch
    html = fetch(item.get("url", ""))
    if not html:
        return item
    meta = videos.all_meta(html)
    genres: list[str] = []
    for key in ("video:tag", "genre", "article:tag"):
        v = meta.get(key)
        for g in (v if isinstance(v, list) else [v] if v else []):
            if g and g not in genres:
                genres.append(g)
    rating = ""
    for node in pagemeta._json_ld_nodes(html):
        g = node.get("genre")
        for x in (g if isinstance(g, list) else [g] if g else []):
            if isinstance(x, str) and x not in genres:
                genres.append(x)
        rating = rating or str(node.get("contentRating") or "")
    rating = rating or str(meta.get("video:rating") or meta.get("rating") or "")
    og_title = pagemeta._meta_one(html, "og:title")
    og_title = re.sub(r"\s*[|–-]\s*(TV3 Play|TV3|play\.tv3\.lv)\s*$", "", og_title).strip()
    out = dict(item)
    out["genres"] = genres[:8]
    out["rating"] = rating
    out["show_title"] = og_title if item.get("kind") != "episode" else item.get("show_title", "")
    if item.get("kind") != "episode" and og_title:
        out["title"] = og_title
    og_img = pagemeta._meta_one(html, "og:image")
    if og_img and "AVOD_META" not in og_img:
        out["poster"] = og_img
    return out


def is_adult(item_or_raw: dict) -> bool:
    rating = str(item_or_raw.get("rating") or "")
    return bool(_ADULT_RE.search(rating))


# --- raksta rindas -------------------------------------------------------------

def is_play_item(article) -> bool:
    return bool(article is not None and (article.raw_json or {}).get("_play"))


def play_data(article) -> dict:
    return dict(((article.raw_json or {}) if article is not None else {}).get("_play") or {})


def link_for(article) -> str:
    return article.canonical_url or article.url


def existing_item(session, item_id: str):
    return session.execute(
        select(Article).where(Article.guid == f"play:{item_id}")).scalar_one_or_none()


def _parse_date(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def upsert_item(session, item: dict, cfg: dict) -> Article | None:
    if not item.get("id") or not item.get("url"):
        return None
    row = existing_item(session, item["id"])
    if row is not None:
        return row
    genres = list(item.get("genres") or [])
    for slug, over in (cfg.get("genre_overrides") or {}).items():
        if item.get("show", "").startswith(slug):
            genres = list(over)
    title = item.get("title") or item.get("show", "").replace("-", " ").title()
    if item.get("kind") == "episode" and item.get("show_title"):
        title = f"{item['show_title']}: {title}"
    row = Article(
        guid=f"play:{item['id']}", url=item["url"], canonical_url=item["url"],
        title=title, lead=item.get("description") or "",
        section=section_for({**item, "genres": genres}, cfg),
        categories=genres,
        images=[u for u in (item.get("poster"), item.get("thumbnail")) if u],
        published_at=_parse_date(item.get("published")) or utcnow(),
        editor_status="can", feed_name=FEED_NAME,
        raw_json={"_play": {"kind": item.get("kind"), "show": item.get("show"),
                            "show_id": item.get("show_id"), "genres": genres,
                            "rating": item.get("rating", ""), "seconds": item.get("seconds", 0),
                            "player": item.get("player", "")},
                  "_section_src": "play",
                  "_page_meta": {"post_types": ["video"], "tags": item.get("tags") or [],
                                 "categories": genres},
                  "_page_meta_at": utcnow().isoformat(timespec="seconds")},
    )
    session.add(row)
    session.flush()
    return row


def crawl(session, rules: dict | None = None, fetch=None, now: datetime | None = None) -> dict:
    """Viens kataloga apgājiens: jaunie nosaukumi kļūst par rindām.
    Izslēgts = nekas netiek ielasīts (arī rindu nav, ko AI lemt)."""
    cfg = settings(rules)
    now = now or utcnow()
    summary = {"enabled": bool(cfg.get("enabled")), "seen": 0, "new": 0, "excluded": 0,
               "at": now.isoformat(timespec="seconds")}
    if not cfg.get("enabled"):
        return summary
    fetch = fetch or pagemeta.fetch
    items = catalog(fetch, cfg, now)
    summary["seen"] = len(items)
    budget = int(cfg.get("max_new_per_run") or 20)
    pages = int(cfg.get("page_fetch_per_run") or 0)
    shows: dict[str, dict] = {}   # raidījuma lapa vienreiz: žanri, cenzs, plakāts, nosaukums
    for item in items:
        if budget <= 0:
            break
        if existing_item(session, item["id"]) is not None:
            continue
        why = excluded(item, cfg)
        if why:
            summary["excluded"] += 1
            continue
        if pages > 0:
            if item["kind"] == "episode":
                show_url = f"{PLAY_HOST}/video/{item['show']}-{item['show_id']}/"
                if item["show_id"] not in shows:
                    shows[item["show_id"]] = enrich_from_page({"url": show_url, "kind": "show"}, fetch)
                    pages -= 1
                show = shows[item["show_id"]]
                # sērija manto raidījuma žanrus, cenzu un plakātu — citādi drūmas
                # dienas sargs to bloķē kā nezināma žanra saturu
                item = {**item, "show_title": show.get("title", ""),
                        "genres": show.get("genres") or [], "rating": show.get("rating", ""),
                        "poster": show.get("poster", "")}
            else:
                item = enrich_from_page(item, fetch)
                pages -= 1
        row = upsert_item(session, item, cfg)
        if row is None:
            continue
        summary["new"] += 1
        budget -= 1
        log.info("Play katalogs: %s «%s» (%s, %s s)", item["kind"], row.title[:60],
                 row.section, item.get("seconds") or "?")
    session.commit()
    import json

    set_setting(session, "play:last", json.dumps(summary, ensure_ascii=False))
    return summary


# --- ētikas sargi ---------------------------------------------------------------

def is_grim(article) -> bool:
    """Traģēdija vai noziegums: AI jutīgums vai drūmi vārdi virsrakstā."""
    if article is None:
        return False
    if any(s in GRIM_SENSITIVITY for s in (article.sensitivity or [])):
        return True
    from app.weekend import grim_words

    return bool(grim_words(article.title or ""))


def paused(session) -> bool:
    return get_setting(session, "play:pause") == "on"


def somber(session, now: datetime | None = None, rules: dict | None = None) -> tuple[bool, float]:
    """(drūma diena?, traģēdiju daļa) — pēc pēdējo stundu publicētajiem un
    ieplānotajiem ierakstiem visos kanālos. Sēru diena vai liela katastrofa
    nozīmē, ka izklaides promo nav vietā."""
    cfg = settings(rules)["somber"]
    now = now or utcnow()
    since = now - timedelta(hours=float(cfg.get("window_hours") or 6))
    rows = session.execute(
        select(Post).where(Post.state.in_(("scheduled", "publishing", "published")),
                           Post.scheduled_at >= since, Post.scheduled_at <= now + timedelta(hours=3))
    ).scalars().all()
    rows = [p for p in rows if p.article is not None and not is_play_item(p.article)
            and not (p.article.raw_json or {}).get("_video")]
    if len(rows) < 3:
        return False, 0.0
    share = sum(1 for p in rows if is_grim(p.article)) / len(rows)
    return share >= float(cfg.get("threshold") or 0.4), round(share, 2)


def genre_ok_on_somber_day(article, rules: dict | None = None) -> bool:
    allowed = [g.lower() for g in settings(rules)["somber"].get("allowed_genres") or []]
    genres = [g.lower() for g in play_data(article).get("genres") or []]
    return bool(genres) and any(any(a in g for a in allowed) for g in genres)


def _riga_day(dt: datetime):
    return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(config.TIMEZONE)).date()


def channel_posts(session, channel: str, since: datetime) -> list[Post]:
    return session.execute(
        select(Post).where(Post.channel == channel,
                           Post.state.in_(("scheduled", "publishing", "published")),
                           Post.scheduled_at >= since)).scalars().all()


def allowed_now(session, article, channel: str, fmt: str = "",
                rules: dict | None = None, now: datetime | None = None) -> tuple[bool, str]:
    """(drīkst?, iemesls) Play nosaukumam šajā kanālā tagad."""
    cfg = settings(rules)
    now = now or utcnow()
    if not cfg.get("enabled"):
        return False, "Play izslēgts (play.enabled)"
    if paused(session):
        return False, "Play pauzēts ar roku"
    is_somber, share = somber(session, now, rules)
    if is_somber and not genre_ok_on_somber_day(article, rules):
        return False, f"drūma diena ({share:.0%} traģēdiju/noziegumu) — tikai mierīgi žanri"
    data = play_data(article)
    week = channel_posts(session, channel, now - timedelta(days=7))
    play_week = [p for p in week if is_play_item(p.article)]
    # tā paša nosaukuma/raidījuma atkārtojums
    cooldown = now - timedelta(days=int(cfg.get("title_cooldown_days") or 14))
    for p in channel_posts(session, channel, cooldown):
        if is_play_item(p.article) and p.article.id != article.id:
            other = play_data(p.article)
            if other.get("show_id") and other.get("show_id") == data.get("show_id"):
                return False, f"«{data.get('show')}» kanālā jau bija pēdējās {cfg.get('title_cooldown_days')} dienās"
    # dienas limiti (Rīgas diena), stāsti atsevišķi
    today = _riga_day(now)
    todays = [p for p in play_week if p.scheduled_at and _riga_day(p.scheduled_at) == today]
    if fmt == "story":
        if sum(1 for p in todays if p.format == "story") >= int(cfg.get("story_daily_cap") or 1):
            return False, "Play stāstu dienas limits"
    else:
        cap = int(cfg.get("weekend_daily_cap") if today.weekday() >= 5 else cfg.get("daily_cap"))
        if sum(1 for p in todays if p.format != "story") >= cap:
            return False, f"Play dienas limits kanālā ({cap})"
    # plūsmas daļa: atļauto Play skaitu noapaļo (10 % no 13 ierakstiem ir 1,
    # ne 0), un sargs dzīvo tikai pie pilnas nedēļas — tukšā kanālā tas
    # nebloķē pirmo promo
    share_cap = float(cfg.get("feed_share") or 0.1)
    allowed = max(1, round(share_cap * len(week)))
    if len(week) >= 10 and len(play_week) >= allowed:
        return False, (f"Play jau ir {len(play_week)}/{len(week)} plūsmas — "
                       f"virs {share_cap:.0%} (atļauti {allowed})")
    return True, ""


def windows_for(article, rules: dict | None = None) -> list[str]:
    cfg = settings(rules)
    if is_adult(play_data(article)):
        return [str(cfg.get("adult_window") or "21:00-23:59")]
    return list(cfg.get("windows") or [])


def too_close_to_grim(queue: list[Post], candidate: datetime, rules: dict | None = None) -> str:
    """Play ieraksts nedrīkst stāvēt blakus traģēdijai vai noziegumam."""
    gap = timedelta(minutes=int(settings(rules).get("adjacency_minutes") or 90))
    for p in queue:
        if p.scheduled_at and abs(p.scheduled_at - candidate) < gap and is_grim(p.article):
            return f"Play pārāk tuvu traģēdijas/nozieguma ierakstam ({(p.article.title or '')[:40]})"
    return ""


def hint(article) -> str:
    d = play_data(article)
    mins = int(d.get("seconds") or 0) // 60
    kind = {"movie": "filma", "show": "raidījums/seriāls", "episode": "sērija"}.get(d.get("kind"), "")
    return (f"šis ir TV3 Play {kind} ({mins} min, žanri: {', '.join(d.get('genres') or []) or 'nav zināmi'}"
            f"{', cenzs ' + d['rating'] if d.get('rating') else ''}): bez maksas Play; "
            "saite ved uz Play lapu; copy ir aicinājums noskatīties, ne ziņa; nekādu saistību ar aktualitātēm")


# --- diagnostika ----------------------------------------------------------------

def summary(session, rules: dict | None = None, now: datetime | None = None) -> dict:
    import json

    cfg = settings(rules)
    items = session.execute(select(Article).where(Article.feed_name == FEED_NAME)).scalars().all()
    since = utcnow() - timedelta(days=7)
    posts = [p for p in session.execute(
        select(Post).where(Post.state == "published", Post.published_at >= since)).scalars().all()
        if is_play_item(p.article)]
    is_somber, share = somber(session, now, rules=rules)
    raw = get_setting(session, "play:last", "")
    try:
        last = json.loads(raw) if raw else {}
    except ValueError:
        last = {}
    return {
        "enabled": bool(cfg.get("enabled")), "paused": paused(session),
        "somber": is_somber, "grim_share": share,
        "items": len(items),
        "items_with_genre": sum(1 for a in items if play_data(a).get("genres")),
        "items_undecided": sum(1 for a in items if a.decided_at is None),
        "published_7d": len(posts),
        "windows": cfg.get("windows"), "daily_cap": cfg.get("daily_cap"),
        "feed_share": cfg.get("feed_share"), "last_crawl": last,
    }
