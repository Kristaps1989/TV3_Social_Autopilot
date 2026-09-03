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

import html as _html
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
    # Sadaļu lapas: TIEŠI te ir katalogs (sākumlapā vien 426 nosaukumu saites).
    # Sitemapi dod jaunās sērijas, sadaļu lapas — pašus nosaukumus.
    "browse_pages": ["/", "/filmas/", "/seriali/", "/sovi-un-raidijumi/",
                     "/berniem/", "/sports/", "/vietejais-saturs/", "/podkasti/"],
    "interval_minutes": 60,
    "max_new_per_run": 20,
    "page_fetch_per_run": 6,          # nosaukumu lapas žanram/cenzam
    # raidījumi, kas ir ziņas, ne izklaide — Play promo tos neņem
    "exclude_slugs": ["tv3-zinas", "tv3-zinas-isuma", "degpunkta", "900-sekundes",
                      "bez-tabu", "neka-personiga", "tiesraides", "video-1"],
    "sport_slugs": ["fiba", "fifa", "wrc", "hokej", "basketbol", "basketball", "futbol",
                    "football", "sport", "olimp", "hockey"],
    # Play lapas vecuma cenzu NEDOD (pārbaudīts ar zondi 07.09.2026), tāpēc
    # pieaugušo saturu atpazīstam pēc adreses/kategorijas — sk. docs/play-strategy.md
    "adult_slugs": ["tikai-pieaugusajiem", "erotika", "erotic", "adult"],
    # Ziņu raidījumi un podkāsti pieder portāla ziņu plūsmai, ne Play promo.
    # Slugu saraksts visus nenoķer (Zviedru Galds, Piķis un ģēvelis), tāpēc
    # šķiro pēc žanra/kategorijas — tas ir noturīgi pret jauniem nosaukumiem.
    "exclude_genres": ["ziņas", "news"],
    # Ziņu sižetus tagad šķiro žanrs, tāpēc garuma slieksnis var būt zems —
    # citādi tas izmestu īsfilmas («Suns Funs un Rīga», 4 min)
    "min_seconds": 120, "min_episode_seconds": 600,
    "daily_cap": 1,                   # darbdienā uz kanālu
    "weekend_daily_cap": 2,
    "story_daily_cap": 1,
    "feed_share": 0.10,               # Play daļa kanāla plūsmā (7 dienas)
    "windows": ["19:00-22:30"],       # vakara logs Rīgā
    "adult_window": "21:00-23:59",    # 16+/18+ tikai vēlu
    "adjacency_minutes": 90,          # attālums no traģēdijas/nozieguma ieraksta
    # Drūmā dienā šķiro AIZLIEGTIE žanri, ne atļautie. Atļauto sarakstu
    # metadatu audits pieķēra kā kļūdu: Play žanru vārdnīca ir plaša un aug
    # (Romantika, Medicīnas, Dzīvesveids & izklaide…), un katrs jauns žanrs
    # sarakstā neesot klusi bloķēja pilnīgi nevainīgu saturu. Aizliegumu
    # saraksts ir īss, saprotams un pats par sevi drošs.
    "somber": {"window_hours": 6, "threshold": 0.4,
               "blocked_genres": ["asa sižet", "šausm", "trilleris", "kara ", "karš",
                                  "noziegum", "detektīv", "katastrof", "vardarb",
                                  "action", "horror", "thriller", "crime", "war",
                                  "disaster", "violence"]},
    "title_cooldown_days": 14,
    # «Pēdējā iespēja»: cik dienas pirms nosaukuma izņemšanas to izceļam un
    # laižam rindas priekšgalā (birka lapā to pasaka arī tieši)
    "last_chance_days": 7,
    "half_life_hours": 72,
    # Kataloga nosaukums nenoveco kā ziņa: 2023. gada filma vakar vakaram
    # der tāpat. 0 = svaiguma ierobežojuma nav; atkārtošanos tur
    # `title_cooldown_days` un prioritātes pusperiods.
    "max_age_hours": 0,
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
# Pieejamības logs nosaukuma lapā: «Pieejams vēl 3 dienas» + birka «Pēdējā iespēja»
_AVAIL_RE = re.compile(
    r"Pieejams\s+(?:vēl\s+)?(\d+)\s*(dien|stund|nedēļ|mēneš)", re.I)
_LAST_CHANCE_RE = re.compile(r"P[eē]d[eē]j[aā]\s+iesp[eē]ja", re.I)
_LABEL_TEXT_RE = re.compile(r'"text"\s*:\s*"([^"]{2,120})"')
# Sezonas un notikuma birkas lapā: «10. SEZONA - FINĀLS», «Jauna sezona»
_SEASON_RE = re.compile(r"(\d{1,2})\.\s*sezona", re.I)
_EVENTS = (("finale", re.compile(r"\bfin[aā]l[sa]?\b", re.I)),
           ("new_season", re.compile(r"jaun[aā]\s+sezon", re.I)),
           ("premiere", re.compile(r"pirmizr[aā]de|premj?[eē]ra|pirm[aā]\s+s[eē]rija", re.I)))
EVENT_LABELS = {"finale": "fināls", "new_season": "jauna sezona",
                "premiere": "pirmizrāde"}
# Nosaukuma lapas saite sadaļu lapā: /filmas/<slug>-<id>/ vai /video/<slug>-<id>/
_ABS_HOST = PLAY_HOST
_TITLE_HREF_RE = re.compile(
    r"href=[\"'](?:https?://(?:www\.)?play\.tv3\.lv)?(/(?:filmas|video|seriali|sovi-un-raidijumi)"
    r"/[^\"'/?#]+-\d+/)[\"']", re.I)


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
    # «fifa-pasaules-kauss--11740256» — slug var beigties ar domuzīmi
    return {"kind": typ, "show": m.group("show").rstrip("-"), "show_id": m.group("show_id"),
            "ep": (m.group("ep") or "").rstrip("-"), "ep_id": ep_id or "",
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


def browse_titles(cfg: dict, fetch) -> list[dict]:
    """Sadaļu lapas -> nosaukumu (filmu, seriālu, raidījumu) saknes lapas.

    Sitemapos ir SĒRIJAS un ziņu sižeti; pats katalogs — filmas un raidījumi —
    ir sadaļu lapās (sākumlapā vien 426 nosaukumu saites). Šeit tie ir bez
    metadatiem, tos pieliek `enrich_from_page`."""
    base = str(cfg.get("base") or PLAY_HOST + "/").rstrip("/")
    out: list[dict] = []
    seen: set[str] = set()
    for path in cfg.get("browse_pages") or []:
        url = path if path.startswith("http") else base + path
        html = fetch(url)
        if not html:
            continue
        for m in _TITLE_HREF_RE.finditer(html):
            full = base + m.group(1)
            loc = parse_loc(full)
            if not loc or loc["kind"] == "episode" or loc["id"] in seen:
                continue
            seen.add(loc["id"])
            out.append({**loc, "url": full, "title": "", "description": "",
                        "thumbnail": "", "seconds": 0, "published": "", "player": "",
                        "tags": [], "source": "browse"})
    return out


def catalog(fetch=None, cfg: dict | None = None, now: datetime | None = None) -> list[dict]:
    """Kataloga vienības: sadaļu lapu nosaukumi + sitemapu jaunās sērijas."""
    from app import videos

    cfg = cfg or settings()
    fetch = fetch or pagemeta.fetch
    by_id: dict[str, dict] = {}
    items: list[dict] = []
    for item in browse_titles(cfg, fetch):
        by_id[item["id"]] = item
        items.append(item)
    for url in sitemap_urls(cfg, now):
        body = fetch(url)
        if not body:
            continue
        for entry in videos.sitemap_video_entries(body):
            loc = parse_loc(entry.get("loc", ""))
            if not loc:
                continue
            known = by_id.get(loc["id"])
            if known is not None:
                # tas pats nosaukums jau no sadaļu lapas: papildinām tikai to,
                # kā tur nav (sīktēls, ilgums, datums)
                for key, val in (("thumbnail", entry.get("thumbnail_loc", "")),
                                 ("seconds", int(entry.get("duration") or 0)
                                  if str(entry.get("duration", "")).isdigit() else 0),
                                 ("published", entry.get("publication_date")
                                  or entry.get("lastmod") or ""),
                                 ("title", entry.get("title", "").strip())):
                    if val and not known.get(key):
                        known[key] = val
                continue
            item = {
                **loc, "url": entry["loc"],
                "title": entry.get("title", "").strip(),
                "description": entry.get("description", "").strip(),
                "thumbnail": entry.get("thumbnail_loc", ""),
                "seconds": int(entry.get("duration") or 0) if str(entry.get("duration", "")).isdigit() else 0,
                "published": entry.get("publication_date") or entry.get("lastmod") or "",
                "player": entry.get("player_loc", ""),
                "tags": entry.get("tags") or [], "source": "sitemap",
            }
            by_id[loc["id"]] = item
            items.append(item)
    return items


def excluded(item: dict, cfg: dict) -> str:
    """Kāpēc vienība nav Play promo materiāls ('' = der)."""
    show = item.get("show", "")
    full = f"{show}-{item.get('show_id', '')}"
    for slug in cfg.get("exclude_slugs") or []:
        if show == slug or show.startswith(slug) or full.startswith(slug):
            return f"ziņu raidījums ({slug})"
    if item.get("kind") not in ("movie", "show", "episode"):
        return "nav filma/seriāls/raidījums"
    secs = int(item.get("seconds") or 0)
    # Nosaukums drīkst būt īss (animēta īsfilma), sērija — nedrīkst: sitemapā
    # «sērijas» ir arī sporta spēļu apskati (81–180 s). Tie nav AVOD saturs,
    # ko vērts izcelt, un tie noveco kā ziņa, kamēr katalogs nenoveco vispār.
    floor = int(cfg.get("min_episode_seconds") or 0) if item.get("kind") == "episode" \
        else int(cfg.get("min_seconds") or 0)
    if secs and secs < floor:
        return f"par īsu ({secs} s) — sižets, ne saturs"
    bad = [g.lower() for g in cfg.get("exclude_genres") or []]
    have = [str(g).lower() for g in
            (list(item.get("genres") or []) + list(item.get("categories") or []))]
    hit = next((g for g in have if g in bad), "")
    if hit:
        return f"ziņu saturs (žanrs «{hit}»)"
    return ""


def section_for(item: dict, cfg: dict) -> str:
    text = " ".join([item.get("show", ""), item.get("title", ""),
                     " ".join(item.get("genres") or []),
                     " ".join(item.get("categories") or [])]).lower()
    if any(s in text for s in (cfg.get("sport_slugs") or [])):
        return "sport"
    return "entertainment"


def _meta_list(meta: dict, *keys: str) -> list[str]:
    out: list[str] = []
    for key in keys:
        v = meta.get(key)
        for x in (v if isinstance(v, list) else [v] if v else []):
            # Play žanrus raksta ar HTML entītijām («Bērniem &amp; ģimenei»),
            # un bez atšifrēšanas tie vārdnīcā parādās divreiz
            x = _html.unescape(str(x)).strip()
            if x and x not in out:
                out.append(x)
    return out


def availability(html: str) -> dict:
    """{expires_days, last_chance} no nosaukuma lapas.

    Play lapā ir atskaite «Pieejams vēl 3 dienas» un sarkana birka «Pēdējā
    iespēja» — tas ir kataloga notikums, ko plāns sauc par «pēdējo iespēju»,
    un vienlaikus vienīgais veids uzzināt, kad nosaukums no Play pazūd.
    """
    text = _html.unescape(re.sub(r"<[^>]+>", " ", html or ""))
    days = None
    m = _AVAIL_RE.search(text)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        days = {"dien": n, "stund": 0, "nedēļ": n * 7, "mēneš": n * 30}.get(unit, n)
    return {"expires_days": days, "last_chance": bool(_LAST_CHANCE_RE.search(text))}


def labels(text: str) -> dict:
    """{season, event} no lapas teksta vai nosaukuma.

    Sērijas lapā ir birka «10. SEZONA - FINĀLS» — kataloga notikums, kas dod
    spēcīgāko iemeslu ierakstam («šovakar fināls»), tāpat kā pirmizrāde vai
    jauna sezona. Notikumu meklējam arī pašā nosaukumā, lai sērijai nebūtu
    jāievelk atsevišķa lapa."""
    clean = _html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    m = _SEASON_RE.search(clean)
    event = next((name for name, rx in _EVENTS if rx.search(clean)), "")
    return {"season": int(m.group(1)) if m else None, "event": event}


def enrich_from_page(item: dict, fetch=None) -> dict:
    """Nosaukuma lapa -> žanri, kategorijas, plakāts, ilgums, sērijas piederība.

    Play lapas (WordPress «skaties» tēma) nes to visu cXense meta tagos, ne
    og:video vai schema.org `genre`; pārbaudīts ar Diagnostikas zondi:

        cXenseParse:zfv-playProductTitle      «Kinozvaigzne un kovbojs»
        cXenseParse:zfv-playProductGenre      «Komēdijas», «Drāmas», «Romantika»
        cXenseParse:zfv-playProductCategories drama, romance, comedy (angliski)
        cXenseParse:zfv-playProductImage3x4   vertikāls plakāts (stāstiem)
        cXenseParse:zfv-playSeriesTitle/Link  kuram raidījumam sērija pieder
        video:duration                        sekundes

    Vecuma cenza lapā NAV — `rating` paliek tukšs, un pieaugušo saturu šķiro
    `adult_slugs`.
    """
    from app import videos

    fetch = fetch or pagemeta.fetch
    html = fetch(item.get("url", ""))
    if not html:
        return item
    meta = videos.all_meta(html)
    px = "cXenseParse:zfv-play"
    # Sadaļu lapās daļa saišu ved uz ŽANRA FILTRA lapām («Filmas – Romantika»),
    # ne uz nosaukumiem: tur nav neviena produkta lauka. Tādas nekļūst par
    # ierakstiem, toties tajās ir īstie nosaukumi — tos paņemam līdzi.
    is_title = bool(meta.get(px + "ProductId") or meta.get(px + "ProductTitle")
                    or str(pagemeta._meta_one(html, "og:type")).startswith("video."))
    out_listing = dict(item)
    out_listing["is_title"] = is_title
    if not is_title:
        og = _html.unescape(pagemeta._meta_one(html, "og:title"))
        out_listing["listing_genre"] = re.split(r"\s+[–—-]\s+", og)[-1].strip()
        out_listing["links"] = [_ABS_HOST + m.group(1)
                                for m in _TITLE_HREF_RE.finditer(html)]
        return out_listing
    genres = _meta_list(meta, px + "ProductGenre", "video:tag", "genre")
    categories = _meta_list(meta, px + "ProductCategories")
    # apraksts: JSON-LD sinopse ir pilna, og:description tikai ievads
    desc, rating, duration = "", "", 0
    for node in pagemeta._json_ld_nodes(html):
        desc = desc or str(node.get("description") or "").strip()
        rating = rating or str(node.get("contentRating") or "")
        duration = duration or videos.parse_duration(node.get("duration"))
        for g in _first_list(node.get("genre")):
            if g not in genres:
                genres.append(g)
    out = dict(item)
    out["is_title"] = True
    out["genres"] = genres[:8]
    out["categories"] = categories[:8]
    out["rating"] = rating or str(meta.get("video:rating") or "")
    title = str(meta.get(px + "ProductTitle") or "").strip()
    if not title:
        title = re.sub(r"\s*\|[^|]*$", "", pagemeta._meta_one(html, "og:title")).strip()
    if title:
        out["title"] = title
    series_title = str(meta.get(px + "SeriesTitle") or "").strip()
    series_link = str(meta.get(px + "SeriesLink") or "").strip()
    if series_title:
        out["show_title"] = series_title
    if series_link:
        out["show_url"] = series_link
    elif item.get("kind") in ("show", "movie"):
        out["show_title"] = title or item.get("show_title", "")
    out["description"] = (desc or pagemeta._meta_one(html, "og:description")
                          or item.get("description", "")).strip()
    # plakāts: vertikālais 3:4 stāstiem un foto, 16:9 rezervē
    poster = str(meta.get(px + "ProductImage3x4") or "")
    wide = str(meta.get(px + "ProductImage16x9") or "") or pagemeta._meta_one(html, "og:image")
    if poster and "AVOD_META" not in poster:
        out["poster"] = poster
    if wide and "AVOD_META" not in wide:
        out["wide_image"] = wide
    secs = duration or videos.parse_duration(meta.get("video:duration"))
    if secs:
        out["seconds"] = secs
    year = str(meta.get(px + "ProductYear") or "")
    if year.isdigit():
        out["year"] = int(year)
    # pieejamības logs un «pēdējā iespēja» — kataloga notikums izlasēm
    out.update(availability(html))
    found = labels(html)
    if found.get("season") and not out.get("season"):
        out["season"] = found["season"]
    if found.get("event") and not out.get("event"):
        out["event"] = found["event"]
    orig = _LABEL_TEXT_RE.search(_html.unescape(
        str(meta.get(px + "ProductLabeloriginalTitle") or "")))
    if orig:
        out["original_title"] = orig.group(1)
    for node in pagemeta._json_ld_nodes(html):
        if node.get("embedUrl"):
            out["embed"] = str(node["embedUrl"])
            break
    out["published"] = (item.get("published")
                        or str(meta.get("video:release_date") or meta.get("datePublished") or ""))
    out["enriched"] = True
    return out


def _first_list(value) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(v).strip() for v in value if isinstance(v, str) and v.strip()]
    return []


def is_adult(data: dict, cfg: dict | None = None) -> bool:
    """Pieaugušo saturs. Play vecuma cenzu nedod, tāpēc: cenzs, ja tāds kādreiz
    parādās, plus adreses/kategoriju saraksts (`adult_slugs`)."""
    cfg = cfg or settings()
    if data.get("adult"):
        return True
    if _ADULT_RE.search(str(data.get("rating") or "")):
        return True
    hay = " ".join([str(data.get("show") or ""), str(data.get("url") or ""),
                    " ".join(data.get("categories") or []),
                    " ".join(data.get("genres") or [])]).lower()
    return any(slug.lower() in hay for slug in (cfg.get("adult_slugs") or []))


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
    categories = list(item.get("categories") or [])
    if not genres and item.get("via_genre"):
        genres = [str(item["via_genre"])]   # žanrs no filtra lapas, kurā to atradām
    for slug, over in (cfg.get("genre_overrides") or {}).items():
        if item.get("show", "").startswith(slug):
            genres = list(over)
    title = item.get("title") or item.get("show", "").replace("-", " ").capitalize()
    if item.get("kind") == "episode" and item.get("show_title"):
        title = f"{item['show_title']}: {title}"
    # attēls: vertikālais plakāts 3:4 (stāstiem un foto), tad platais, tad sīktēls
    images = [u for u in (item.get("poster"), item.get("wide_image"), item.get("thumbnail")) if u]
    data = {"kind": item.get("kind"), "show": item.get("show"),
            "show_id": item.get("show_id"), "show_url": item.get("show_url", ""),
            "genres": genres, "categories": categories,
            "rating": item.get("rating", ""), "seconds": item.get("seconds", 0),
            "year": item.get("year"), "player": item.get("player", ""),
            "original_title": item.get("original_title", ""),
            "season": item.get("season"), "event": item.get("event", ""),
            "embed": item.get("embed", ""), "url": item["url"]}
    # cik ilgi nosaukums Play vēl būs: pēc tam saite ved uz «nav pieejams»
    days = item.get("expires_days")
    if days is not None:
        data["expires_at"] = (utcnow() + timedelta(days=int(days))).isoformat(timespec="seconds")
        data["last_chance"] = bool(item.get("last_chance")
                                   or int(days) <= int(cfg.get("last_chance_days") or 7))
    elif item.get("last_chance"):
        data["last_chance"] = True
    data["adult"] = is_adult({**data, "url": item["url"]}, cfg)
    row = Article(
        guid=f"play:{item['id']}", url=item["url"], canonical_url=item["url"],
        title=title, lead=item.get("description") or "",
        section=section_for({**item, "genres": genres, "categories": categories}, cfg),
        categories=genres or categories,
        images=images,
        published_at=_parse_date(item.get("published")) or utcnow(),
        editor_status="can", feed_name=FEED_NAME,
        raw_json={"_play": data, "_section_src": "play",
                  "_page_meta": {"post_types": ["video"], "tags": item.get("tags") or [],
                                 "categories": genres or categories},
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
    summary["deferred"] = 0
    summary["listings"] = 0
    budget = int(cfg.get("max_new_per_run") or 20)
    pages = int(cfg.get("page_fetch_per_run") or 0)
    shows: dict[str, dict] = {}   # raidījuma lapa vienreiz: žanri, plakāts, nosaukums
    queued = {i["id"] for i in items}
    for item in items:
        if budget <= 0:
            break
        if existing_item(session, item["id"]) is not None:
            continue
        why = excluded(item, cfg)
        if why:
            summary["excluded"] += 1
            continue
        if item.get("kind") == "episode":
            # sērija manto raidījuma žanrus un plakātu — citādi drūmās dienas
            # sargs to bloķē kā nezināma žanra saturu
            show_url = item.get("show_url") or f"{PLAY_HOST}/video/{item['show']}-{item['show_id']}/"
            if item["show_id"] not in shows:
                if pages <= 0:
                    summary["deferred"] += 1
                    continue
                shows[item["show_id"]] = enrich_from_page({"url": show_url, "kind": "show"}, fetch)
                pages -= 1
            show = shows[item["show_id"]]
            # sērijas paša notikums (fināls, pirmizrāde) nāk no tās nosaukuma —
            # raidījuma lapas birka pieder citai sērijai
            own = labels(" ".join([item.get("title", ""), item.get("ep", ""),
                                   item.get("description", "")]))
            item = {**item, "show_title": show.get("title", "") or item.get("show_title", ""),
                    "genres": show.get("genres") or [],
                    "categories": show.get("categories") or [],
                    "rating": show.get("rating", ""),
                    "poster": show.get("poster", ""), "show_url": show_url,
                    "season": own.get("season") or show.get("season"),
                    "event": own.get("event") or ""}
        else:
            # sadaļu lapas nosaukums nāk bez metadatiem: bez lapas ielasīšanas
            # tam nav ne žanra, ne plakāta — labāk atlikt uz nākamo apgājienu
            if pages <= 0:
                summary["deferred"] += 1
                continue
            item = enrich_from_page(item, fetch)
            pages -= 1
            # Žanru zinām tikai PĒC lapas ielasīšanas, tāpēc ziņu raidījumus
            # («Zviedru Galds», «Piķis un ģēvelis») šķirojam vēlreiz šeit —
            # pirmā pārbaude notika, kad žanra vēl nebija.
            why = excluded(item, cfg)
            if item.get("is_title", True) and why:
                summary["excluded"] += 1
                log.info("Play katalogs: izlaists %s — %s", item.get("url", ""), why)
                continue
            if not item.get("is_title", True):
                # žanra filtra lapa («Filmas – Romantika»): pati par ierakstu
                # nekļūst, bet tajā ir īstie nosaukumi — un lapas žanrs tiem
                # noder kā rezerve, ja nosaukuma lapa savu nedod
                summary["listings"] += 1
                genre = item.get("listing_genre", "")
                for url in (item.get("links") or [])[:60]:
                    loc = parse_loc(url)
                    if (not loc or loc["kind"] == "episode" or url == item["url"]
                            or loc["id"] in queued or existing_item(session, loc["id"])):
                        continue
                    queued.add(loc["id"])
                    items.append({**loc, "url": url, "title": "", "description": "",
                                  "thumbnail": "", "seconds": 0, "published": "",
                                  "player": "", "tags": [], "source": "listing",
                                  "via_genre": genre})
                continue
        row = upsert_item(session, item, cfg)
        if row is None:
            continue
        summary["new"] += 1
        budget -= 1
        log.info("Play katalogs: %s «%s» (%s, žanri %s, %s s)", item.get("kind"),
                 row.title[:60], row.section, ", ".join(item.get("genres") or []) or "-",
                 item.get("seconds") or "?")
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


def somber_blocked(genres, rules: dict | None = None) -> str:
    """Pirmais žanrs, kas drūmā dienā nav vietā ('' ja tādu nav)."""
    bad = [g.lower() for g in settings(rules)["somber"].get("blocked_genres") or []]
    for g in genres or []:
        low = str(g).lower()
        if any(b in low for b in bad):
            return str(g)
    return ""


def genre_ok_on_somber_day(article, rules: dict | None = None) -> bool:
    """Drūmā dienā der viss, izņemot asa sižeta, šausmu, kara un noziegumu
    saturu. Nosaukums bez neviena žanra der arī nē: to pārbaudīt nevaram."""
    data = play_data(article)
    genres = list(data.get("genres") or []) + list(data.get("categories") or [])
    return bool(genres) and not somber_blocked(genres, rules)


def expired(article, now: datetime | None = None) -> bool:
    """Nosaukums Play vairs nav pieejams (pēc lapas atskaites «Pieejams vēl …»).
    Bez šī saite pēc dažām dienām vestu uz «nav pieejams» lapu."""
    stamp = play_data(article).get("expires_at")
    if not stamp:
        return False
    try:
        return datetime.fromisoformat(str(stamp)) <= (now or utcnow())
    except ValueError:
        return False


def last_chance(article) -> bool:
    return bool(play_data(article).get("last_chance")) and not expired(article)


def event_label(article) -> str:
    """«fināls» / «jauna sezona» / «pirmizrāde» / «pēdējā iespēja» — kataloga
    notikums, kas dod ierakstam iemeslu tieši šodien ('' ja tāda nav)."""
    d = play_data(article)
    label = EVENT_LABELS.get(str(d.get("event") or ""), "")
    if label and d.get("season") and d["event"] in ("finale", "new_season"):
        return f"{d['season']}. sezonas {label}"
    return label or ("pēdējā iespēja" if last_chance(article) else "")


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
    if expired(article, now):
        return False, "nosaukums Play vairs nav pieejams"
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
    if is_adult({**play_data(article), "url": article.canonical_url or article.url}, cfg):
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
    season = f", {d['season']}. sezona" if d.get("season") else ""
    mins = int(d.get("seconds") or 0) // 60
    kind = {"movie": "filma", "show": "raidījums/seriāls", "episode": "sērija"}.get(d.get("kind"), "")
    genres = ", ".join(d.get("genres") or d.get("categories") or []) or "nav zināmi"
    return (f"šis ir TV3 Play {kind} ({mins} min{season}, žanri: {genres}"
            f"{', ' + str(d['year']) if d.get('year') else ''}"
            f"{', cenzs ' + d['rating'] if d.get('rating') else ''}"
            f"{'; NOTIKUMS: ' + event_label(article).upper() if event_label(article) else ''}"
            f"{' — drīz pazūd no Play' if d.get('last_chance') else ''}"
            f"): bez maksas Play; "
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
        "overrides": rule_overrides(rules),
        "somber": is_somber, "grim_share": share,
        "items": len(items),
        "items_with_genre": sum(1 for a in items if play_data(a).get("genres")),
        "items_last_chance": sum(1 for a in items if last_chance(a)),
        "items_events": sum(1 for a in items if play_data(a).get("event")),
        "items_expired": sum(1 for a in items if expired(a)),
        "items_undecided": sum(1 for a in items if a.decided_at is None),
        "published_7d": len(posts),
        "audit": last_audit(session),
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
    # Steidzamība pa priekšu: vispirms «pēdējā iespēja» (pēc dažām dienām
    # nosaukuma Play vairs nav), tad pārējie notikumi (fināls, pirmizrāde)
    def _rank(a) -> int:
        return 0 if last_chance(a) else (1 if event_label(a) else 2)

    ordered = sorted(rows, key=lambda a: (_rank(a),
                                          -(1 if a.images else 0),
                                          -scores.get(str(play_data(a).get("show_id")), 0.0),
                                          -(a.published_at or utcnow()).timestamp()))
    for a in ordered:
        d = play_data(a)
        if d.get("kind") not in ("movie", "show", "episode") or expired(a, now):
            continue
        sid = str(d.get("show_id") or "")
        if not sid or sid in seen_shows or sid in recent_shows or is_adult(d, cfg):
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
        subtitles.append(" · ".join(x for x in (
            g, f"{mins} min" if mins else "", event_label(a)) if x))
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


# --- metadatu audits (Diagnostika) ----------------------------------------------

AUDIT_FIELDS = ("title", "genres", "categories", "poster", "seconds", "year",
                "description", "season", "event", "last_chance", "expires_days",
                "rating", "show_title")


def _page_links(html: str, base: str) -> list[str]:
    out: list[str] = []
    for m in _TITLE_HREF_RE.finditer(html or ""):
        url = base + m.group(1)
        loc = parse_loc(url)
        if loc and loc["kind"] != "episode" and url not in out:
            out.append(url)
    return out


def _spread(items: list[str], count: int) -> list[str]:
    """Vienmērīgi izkliedēts paraugs, ne pirmie N — saraksta sākums sadaļās
    mēdz būt viens un tas pats (izceltie raidījumi)."""
    if count <= 0 or not items:
        return []
    if len(items) <= count:
        return list(items)
    step = len(items) / count
    return [items[int(i * step)] for i in range(count)]


def _audit_row(info: dict) -> dict:
    """Viena parauga īsais kopsavilkums (bez pilnas lapas)."""
    return {"url": info.get("url", ""), "kind": info.get("kind", ""),
            "title": info.get("title", ""),
            "genres": info.get("genres") or [], "categories": info.get("categories") or [],
            "seconds": info.get("seconds") or 0, "year": info.get("year"),
            "season": info.get("season"), "event": info.get("event", ""),
            "last_chance": bool(info.get("last_chance")),
            "expires_days": info.get("expires_days"),
            "rating": info.get("rating", ""), "poster": bool(info.get("poster")),
            "description": bool(info.get("description"))}


def rule_overrides(rules: dict | None = None) -> dict:
    """Kur DZĪVAIS `play` bloks atšķiras no repo noklusējuma faila.

    Rediģējamā kopija uz servera tiek uzsēta VIENU reizi. `sync_missing_rules`
    pieliek jaunas augšējā līmeņa atslēgas, bet `play` bloks tur jau ir, tāpēc
    izmaiņas tā iekšienē (piem., `min_seconds` vai `browse_pages`) uz servera
    nekad nenonāk. Bez šī saraksta tas ir neredzams: kods saka vienu, sistēma
    dara citu. Atšķirība nav kļūda — redaktors drīkst pārrakstīt jebkuru
    vērtību —, bet to vajag redzēt.
    """
    live = settings(rules)
    try:
        shipped = settings(config._load_yaml(config.DEFAULT_RULES_DIR / "rules.yaml"))
    except Exception:  # noqa: BLE001 — audits nedrīkst krist konfigurācijas dēļ
        return {}
    out: dict = {}
    for key, want in shipped.items():
        if key == "enabled":   # slēdzi drīkst pārslēgt bez brīdinājuma
            continue
        if live.get(key) != want:
            out[key] = {"live": live.get(key), "code": want}
    return out


def audit(fetch=None, rules: dict | None = None, per_section: int = 4,   # noqa: C901
          episodes: int = 3, now: datetime | None = None) -> dict:
    """Pilns metadatu pārskats pa VISĀM Play sadaļām.

    Katrā sadaļā paņem dažus nosaukumus, ielasa lapas un saskaita, kuri lauki
    tur tiešām ir. Tas aizstāj ekrānuzņēmumu sūtīšanu pa vienam: uzreiz redzams,
    kurās sadaļās trūkst žanra (drūmā dienā tādus nepublicē), vai kaut kur
    parādās vecuma cenzs, un kāda ir īstā žanru vārdnīca.
    """
    cfg = settings(rules)
    fetch = fetch or pagemeta.fetch
    base = str(cfg.get("base") or PLAY_HOST + "/").rstrip("/")
    out: dict = {"at": (now or utcnow()).isoformat(timespec="seconds"),
                 "sections": [], "genres": {}, "categories": {}, "events": {},
                 "field_coverage": {}, "warnings": []}
    rows: list[dict] = []
    # Katrā lapā ir kopīga izceltā josla (Bez Tabu, Degpunktā u. c.). Ja to
    # neizmet, visās sadaļās paraugā nonāk vieni un tie paši četri raidījumi.
    per_page: dict[str, tuple[bool, list[str]]] = {}
    for path in cfg.get("browse_pages") or []:
        url = path if path.startswith("http") else base + path
        html = fetch(url) or ""
        per_page[path] = (bool(html), _page_links(html, base))
    seen_on: dict[str, int] = {}
    for _, links in per_page.values():
        for u in set(links):
            seen_on[u] = seen_on.get(u, 0) + 1
    chrome = {u for u, n in seen_on.items() if n >= 3}
    out["chrome_links"] = sorted(chrome)[:20]
    for path in cfg.get("browse_pages") or []:
        ok, links = per_page.get(path, (False, []))
        section: dict = {"path": path, "fetched": ok, "titles_found": 0,
                         "sampled": 0, "fields": {}, "samples": []}
        if links:
            found = [u for u in links if u not in chrome]
            section["titles_found"] = len(found)
            section["chrome_skipped"] = len(links) - len(found)
            for title_url in _spread(found, per_section):
                info = enrich_from_page({**parse_loc(title_url), "url": title_url}, fetch)
                row = {**_audit_row(info), "is_title": bool(info.get("is_title", True)),
                       "listing_genre": info.get("listing_genre", "")}
                section["samples"].append(row)
                rows.append({**row, "section": path})
            section["sampled"] = len(section["samples"])
            section["fields"] = {f: sum(1 for r in section["samples"]
                                        if r.get(f) not in (None, "", [], 0, False))
                                 for f in AUDIT_FIELDS if f in (section["samples"] or [{}])[0]}
        out["sections"].append(section)
    # sērijas atsevišķi: tām lapa izskatās citādi (sezona, fināls)
    ep_rows: list[dict] = []
    from app import videos

    for sm in sitemap_urls(cfg, now)[:1]:
        body = fetch(sm)
        for entry in videos.sitemap_video_entries(body or ""):
            loc = parse_loc(entry.get("loc", ""))
            if not loc or loc["kind"] != "episode" or excluded(loc, cfg):
                continue
            info = enrich_from_page({**loc, "url": entry["loc"],
                                     "title": entry.get("title", "")}, fetch)
            info.update(labels(" ".join([info.get("title", ""), loc.get("ep", "")])) or {})
            ep_rows.append({**_audit_row(info), "section": "sērijas"})
            if len(ep_rows) >= episodes:
                break
    if ep_rows:
        out["sections"].append({"path": "sērijas (no sitemap)", "fetched": True,
                                "titles_found": len(ep_rows), "sampled": len(ep_rows),
                                "fields": {}, "samples": ep_rows})
        rows.extend(ep_rows)

    # Žanra filtra lapas nav nosaukumi: tās neskaita pie lauku pārklājuma,
    # citādi «žanrs 45 %» nozīmētu tikai to, ka paraugā bija filtru lapas.
    listings = [r["url"] for r in rows if r.get("is_title") is False]
    out["listing_pages"] = listings
    if listings:
        out["warnings"].append(
            f"{len(listings)} paraugi ir žanra filtra lapas, ne nosaukumi — tās "
            "nekļūst par ierakstiem, bet no tām paņem īstos nosaukumus")
    rows = [r for r in rows if r.get("is_title") is not False]
    # viens nosaukums var būt vairākās sadaļās — kopskaitam to skaitām vienreiz
    seen_urls: set[str] = set()
    rows = [r for r in rows if not (r["url"] in seen_urls or seen_urls.add(r["url"]))]
    total = len(rows)
    out["sampled_total"] = total
    if total:
        keys = ("title", "genres", "categories", "poster", "seconds", "year",
                "description", "season", "event", "last_chance", "expires_days", "rating")
        out["field_coverage"] = {
            k: {"count": sum(1 for r in rows if r.get(k) not in (None, "", [], 0, False)),
                "pct": round(100.0 * sum(1 for r in rows
                                         if r.get(k) not in (None, "", [], 0, False)) / total)}
            for k in keys}
    for r in rows:
        for g in r.get("genres") or []:
            out["genres"][g] = out["genres"].get(g, 0) + 1
        for c in r.get("categories") or []:
            out["categories"][c] = out["categories"].get(c, 0) + 1
        if r.get("event"):
            out["events"][r["event"]] = out["events"].get(r["event"], 0) + 1
    out["genres"] = dict(sorted(out["genres"].items(), key=lambda kv: -kv[1]))
    out["categories"] = dict(sorted(out["categories"].items(), key=lambda kv: -kv[1]))

    # ko tas nozīmē sargiem
    unmapped = [g for g in out["genres"] if somber_blocked([g], rules)]
    out["somber_blocked_genres"] = unmapped
    no_genre = [r["url"] for r in rows if not (r.get("genres") or r.get("categories"))]
    out["titles_without_genre"] = no_genre
    if no_genre:
        out["warnings"].append(
            f"{len(no_genre)} no {total} nosaukumiem nav žanra — drūmā dienā tie tiks "
            "bloķēti; pieliec žanru ar `genre_overrides`")
    if unmapped:
        out["warnings"].append(
            "šos žanrus drūmā dienā nepublicē (asa sižeta, šausmu, noziegumu u. tml.): "
            + ", ".join(unmapped[:12]))
    if not any(r.get("rating") for r in rows):
        out["warnings"].append(
            "nevienā paraugā nav vecuma cenza — 16+/18+ šķirošana balstās tikai uz "
            "`adult_slugs`; vērts lūgt Play komandai cenzu pievienot lapā")
    empty = [s["path"] for s in out["sections"] if s["fetched"] and not s["titles_found"]]
    if empty:
        out["warnings"].append("sadaļās bez nosaukumiem (varbūt cita adrese): "
                               + ", ".join(empty))
    missing = [s["path"] for s in out["sections"] if not s.get("fetched")]
    if missing:
        out["warnings"].append("sadaļas neatbild: " + ", ".join(missing))
    out["rule_overrides"] = rule_overrides(rules)
    if out["rule_overrides"]:
        out["warnings"].append(
            "rediģējamā rules.yaml kopija atšķiras no koda noklusējuma "
            "(izmaiņas `play` blokā uz servera nenonāk pašas): "
            + ", ".join(sorted(out["rule_overrides"])))
    return out


def save_audit(session, data: dict) -> None:
    """Īsais kopsavilkums Diagnostikas lapai (Setting.value ir īss lauks)."""
    import json

    cov = data.get("field_coverage") or {}
    set_setting(session, "play:audit", json.dumps({
        "at": data.get("at", ""), "sampled": data.get("sampled_total", 0),
        "genres": len(data.get("genres") or {}),
        "genre_pct": (cov.get("genres") or {}).get("pct", 0),
        "poster_pct": (cov.get("poster") or {}).get("pct", 0),
        "rating_pct": (cov.get("rating") or {}).get("pct", 0),
        "warnings": len(data.get("warnings") or []),
        "overrides": len(data.get("rule_overrides") or {}),
    }, ensure_ascii=False)[:250])


def last_audit(session) -> dict:
    import json

    raw = get_setting(session, "play:audit", "")
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}

