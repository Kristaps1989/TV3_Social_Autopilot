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
    # P2: izlašu karuselis (3–5 nosaukumi, katra kartīte ar savu saiti)
    "selection_channel": "fb_tv3lv",
    "selection_days": [4, 5, 6],      # piektdiena, sestdiena, svētdiena
    "selection_build_hour": 17,       # būvē no 17:00, publicē vakara logā
    "selection_hour": 19,
    "selection_size": 5,
    "selection_requires_approval": True,   # pirmajā mēnesī redaktors apstiprina
    # P2: entītiju tilti — raksts par raidījumu/personu ved uz Play nosaukumu
    "bridges": True,
    "bridge_sections": ["entertainment", "sport"],
    "bridge_cooldown_days": 3,
    # P3: maksas pastiprināšana tikai organiski strādājošiem ierakstiem
    "boost_min_impressions": 1000,
    "boost_min_clicks": 10,
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
    bridged = [a for a in session.execute(
        select(Article).where(Article.raw_json["_play_bridge"].as_string() != "")).scalars().all()
        if (a.raw_json or {}).get("_play_bridge")]
    selections = session.execute(
        select(Post).where(Post.hook_type == SELECTION_MARKER,
                           Post.state.in_(("proposed", "scheduled", "published")))).scalars().all()
    scores = title_scores(session)
    return {
        "bridged_articles": len(bridged),
        "selections": len(selections),
        "selections_pending": sum(1 for p in selections if p.state == "proposed"),
        "top_titles": sorted(scores.items(), key=lambda kv: -kv[1])[:5],
        "enabled": bool(cfg.get("enabled")), "paused": paused(session),
        "somber": is_somber, "grim_share": share,
        "items": len(items),
        "items_with_genre": sum(1 for a in items if play_data(a).get("genres")),
        "items_undecided": sum(1 for a in items if a.decided_at is None),
        "published_7d": len(posts),
        "windows": cfg.get("windows"), "daily_cap": cfg.get("daily_cap"),
        "feed_share": cfg.get("feed_share"), "last_crawl": last,
    }


# --- P3: nosaukumu prioritātes no mērījumiem -----------------------------------

def title_scores(session, days: int = 60) -> dict[str, float]:
    """show_id -> sesijas uz 1000 sasniegtajiem (GA4 + platformu insights) no
    Play ierakstiem. Bez GA4 Play notikumiem tas ir tuvākais «skatīšanās
    sākumam», kas mums ir; tiklīdz GA4 dod video_start, te maina avotu."""
    from sqlalchemy import func

    from app.models import PostMetrics

    since = utcnow() - timedelta(days=days)
    rows = session.execute(
        select(Post, func.max(PostMetrics.impressions), func.max(PostMetrics.clicks),
               func.max(PostMetrics.ga_sessions))
        .join(PostMetrics, PostMetrics.post_id == Post.id)
        .where(Post.state == "published", Post.published_at >= since)
        .group_by(Post.id)).all()
    agg: dict[str, list[float]] = {}
    for post, imp, clicks, sess in rows:
        if not is_play_item(post.article):
            continue
        sid = str(play_data(post.article).get("show_id") or "")
        if not sid:
            continue
        a = agg.setdefault(sid, [0.0, 0.0])
        a[0] += float(imp or 0)
        a[1] += float(max(sess or 0, clicks or 0))
    return {sid: round(1000.0 * v[1] / v[0], 2) for sid, v in agg.items() if v[0] >= 200}


# --- P2: izlašu karuselis ------------------------------------------------------

SELECTION_MARKER = "playselection"
THEMES = {4: "Piektdienas vakaram", 5: "Sestdienas izlase", 6: "Svētdienas izlase"}


def _norm(text: str) -> str:
    import unicodedata

    t = unicodedata.normalize("NFKD", str(text or "").lower())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def _first_genre(article) -> str:
    g = play_data(article).get("genres") or []
    return _norm(g[0]) if g else ""


def selection_candidates(session, cfg: dict, now: datetime, channel: str) -> list[Article]:
    """Nosaukumi izlasei: pa vienam uz raidījumu, bez nesen rādītiem, bez
    16+/18+ (vakars sākas 19:00), drūmā dienā tikai mierīgi žanri, žanru
    dažādība, priekšroka ar plakātu un labāku mērīto rezultātu."""
    rows = session.execute(
        select(Article).where(Article.feed_name == FEED_NAME)
        .order_by(Article.published_at.desc())).scalars().all()
    is_somber, _ = somber(session, now)
    cooldown = now - timedelta(days=int(cfg.get("title_cooldown_days") or 14))
    recent_shows = {str(play_data(p.article).get("show_id") or "")
                    for p in channel_posts(session, channel, cooldown) if is_play_item(p.article)}
    for p in channel_posts(session, channel, cooldown):
        for it in ((p.extra or {}).get("items") or []):
            if isinstance(it, dict) and it.get("show_id"):
                recent_shows.add(str(it["show_id"]))
    scores = title_scores(session)
    seen_shows: set[str] = set()
    picked: list[Article] = []
    per_genre: dict[str, int] = {}
    ordered = sorted(rows, key=lambda a: (-(1 if a.images else 0),
                                          -scores.get(str(play_data(a).get("show_id")), 0.0),
                                          -(a.published_at or utcnow()).timestamp()))
    for a in ordered:
        d = play_data(a)
        if d.get("kind") not in ("movie", "show", "episode"):
            continue
        sid = str(d.get("show_id") or "")
        if not sid or sid in seen_shows or sid in recent_shows or is_adult(d):
            continue
        if is_somber and not genre_ok_on_somber_day(a):
            continue
        g = _first_genre(a)
        if g and per_genre.get(g, 0) >= 2:
            continue
        seen_shows.add(sid)
        per_genre[g] = per_genre.get(g, 0) + 1
        picked.append(a)
        if len(picked) >= int(cfg.get("selection_size") or 5):
            break
    return picked


def _show_page(article) -> str:
    d = play_data(article)
    if d.get("kind") == "episode" and d.get("show") and d.get("show_id"):
        return f"{PLAY_HOST}/video/{d['show']}-{d['show_id']}/"
    return article.canonical_url or article.url


def _display_title(article) -> str:
    t = article.title or ""
    return t.split(":")[0].strip() if play_data(article).get("kind") == "episode" and ":" in t else t


def build_selection(session, day, now: datetime | None = None, rules: dict | None = None) -> Post | None:
    """Izlases karuselis: 3–5 Play nosaukumi, katra kartīte ar savu saiti un
    savu utm_term, saraksts pirmajā komentārā. Bloķējošie apstākļi: Play
    izslēgts/pauzēts, par maz nosaukumu, renderētājs, drūma diena bez
    mierīgiem žanriem, traģēdija plūsmā blakus slotam."""
    from app import cards, weekend

    cfg = settings(rules)
    now = now or utcnow()
    if not cfg.get("enabled") or paused(session):
        return None
    channel = str(cfg.get("selection_channel") or "fb_tv3lv")
    picked = selection_candidates(session, cfg, now, channel)
    if len(picked) < 3:
        log.info("Play izlase %s: par maz nosaukumu (%d)", day, len(picked))
        return None
    if not cards.renderer_available():
        return None
    theme = THEMES.get(day.weekday(), "Vakara izlase")
    title = f"{theme}: TV3 Play"
    points = [_display_title(a) for a in picked]
    images = [(a.images or [""])[0] for a in picked]
    subtitles = []
    for a in picked:
        d = play_data(a)
        mins = int(d.get("seconds") or 0) // 60
        g = (d.get("genres") or [""])[0]
        subtitles.append(" · ".join(x for x in (g, f"{mins} min" if mins else "") if x))
    try:
        media = cards.render_cards(
            title, "entertainment", "#TV3PLAY", points, "", "",
            date_txt=day.strftime("%d.%m.%Y"), point_images=images,
            point_blur=["" for _ in picked], point_dates=subtitles,
            include_cover=False, include_end=False, label="TV3 PLAY · BEZ MAKSAS")
    except Exception as e:  # noqa: BLE001
        log.warning("Play izlases renders neizdevās: %s", e)
        cards.record_render_failure("play_selection", e)
        return None
    if not media:
        return None
    used = picked[:len(media)]
    slot = weekend._local_slot(day, int(cfg.get("selection_hour") or 19)) + timedelta(minutes=30)
    from app import slots as _slots

    queue = _slots._channel_queue(session, channel, slot)
    for _ in range(4):
        if not too_close_to_grim(queue, slot, rules):
            break
        slot += timedelta(minutes=45)
    else:
        log.info("Play izlase %s: plūsmā ap vakaru ir traģēdijas — šodien nē", day)
        return None
    items = [{"title": _display_title(a), "url": _show_page(a),
              "show_id": play_data(a).get("show_id"), "article": a.id} for a in used]
    copy = (f"{theme} — {len(used)} filmas un seriāli, ko skatīties bez maksas TV3 Play. "
            f"Saites komentārā.")
    guid = f"play-selection-{day.isoformat()}"
    art = weekend._digest_article(session, guid, title, "entertainment", items[0]["url"])
    art.raw_json = {**(art.raw_json or {}), "_play": {"kind": "selection", "show_id": "",
                                                       "genres": ["izlase"]}}
    post = weekend._schedule(session, art, "card_carousel", copy, media, items[0]["url"],
                             SELECTION_MARKER, slot, channel=channel,
                             card_links=[it["url"] for it in items],
                             card_titles=[it["title"] for it in items], items=items,
                             recipe={"kind": "play_selection", "theme": theme,
                                     "articles": [a.id for a in used]})
    if cfg.get("selection_requires_approval", True):
        post.state = "proposed"
    session.commit()
    set_setting(session, f"play:selection:{day.isoformat()}", str(post.id))
    log.info("Play izlase %s: %d nosaukumi, ieraksts %s (%s)", day, len(used), post.id, post.state)
    return post


def tick(session, now: datetime | None = None) -> dict:
    """Stundas solis: katalogs + izlase izvēlētajās dienās (vienreiz dienā)."""
    now = now or utcnow()
    out = {"crawl": crawl(session, now=now), "selection": None}
    cfg = settings()
    if not cfg.get("enabled"):
        return out
    local = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(config.TIMEZONE))
    day = local.date()
    if (local.weekday() in (cfg.get("selection_days") or [])
            and local.hour >= int(cfg.get("selection_build_hour") or 17)
            and not get_setting(session, f"play:selection:{day.isoformat()}", "")):
        post = build_selection(session, day, now)
        if post is None:
            set_setting(session, f"play:selection:{day.isoformat()}", "skip")
        out["selection"] = post.id if post is not None else "skip"
    return out


# --- P2: entītiju tilti ----------------------------------------------------------

def show_index(session) -> list[dict]:
    """Raidījumu/filmu nosaukumi tiltiem: {show_id, name, norm, url}."""
    rows = session.execute(select(Article).where(Article.feed_name == FEED_NAME)).scalars().all()
    out: dict[str, dict] = {}
    for a in rows:
        d = play_data(a)
        sid = str(d.get("show_id") or "")
        if not sid or d.get("kind") not in ("movie", "show", "episode"):
            continue
        name = _display_title(a)
        norm = _norm(name)
        if len(norm) < 6 or (len(norm.split()) < 2 and len(norm) < 8):
            continue   # «Leila», «Bruno» — par īsu, lai droši sakristu
        if sid not in out:
            out[sid] = {"show_id": sid, "name": name, "norm": norm, "url": _show_page(a),
                        "genres": d.get("genres") or []}
    return list(out.values())


def bridge_for_article(session, article, now: datetime | None = None,
                       rules: dict | None = None) -> dict | None:
    """Tilts no raksta uz Play nosaukumu — tikai pēc entītijas sakritības un
    tikai pozitīvā/neitrālā kontekstā. Nekad no traģēdijas vai nozieguma,
    nekad drūmā dienā, ne biežāk kā reizi 3 dienās uz raidījumu."""
    cfg = settings(rules)
    now = now or utcnow()
    if not (cfg.get("enabled") and cfg.get("bridges")) or paused(session):
        return None
    if article is None or is_play_item(article) or (article.raw_json or {}).get("_digest"):
        return None
    if article.section not in (cfg.get("bridge_sections") or []):
        return None
    if is_grim(article) or (article.sensitivity or []):
        return None
    if somber(session, now, rules)[0]:
        return None
    hay = _norm(article.title) + " " + _norm(" ".join(pagemeta.tags(article, 10)))
    best = None
    for show in show_index(session):
        if f" {show['norm']} " in f" {hay} " and (best is None or len(show["norm"]) > len(best["norm"])):
            best = show
    if best is None:
        return None
    key = f"play:bridge:{best['show_id']}"
    last = get_setting(session, key, "")
    if last:
        try:
            if now - datetime.fromisoformat(last) < timedelta(days=int(cfg.get("bridge_cooldown_days") or 3)):
                return None
        except ValueError:
            pass
    bridge = {"show_id": best["show_id"], "title": best["name"], "url": best["url"]}
    article.raw_json = {**(article.raw_json or {}), "_play_bridge": bridge}
    set_setting(session, key, now.isoformat(timespec="seconds"))
    log.info("Play tilts: raksts %s -> «%s»", article.id, best["name"])
    return bridge


def bridge_line(post, platform: str) -> str:
    """Rinda ieraksta komentārā/aprakstā: «Skaties … bez maksas TV3 Play»."""
    from app.best_practices import add_utm

    article = getattr(post, "article", None)
    if article is None or is_play_item(article):
        return ""
    bridge = (article.raw_json or {}).get("_play_bridge") or {}
    if not bridge.get("url"):
        return ""
    url = add_utm(bridge["url"], platform, post.id, hook="bridge",
                  campaign=str(settings().get("campaign") or "play"))
    return f"▶ Skaties «{bridge.get('title', '')}» bez maksas TV3 Play: {url}"

