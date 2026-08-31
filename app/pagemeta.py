"""Raksta lapas metadati: tas, ko feed nenes.

Katrs tv3.lv raksts GTM dataLayer'ā iepush'o `dlEvent` objektu ar paša CMS
metadatiem — Post ID, redaktora vārds, tagi, kategoriju koks, ieraksta tips
(``Video;Gallery``), satura garums, zīmols (``Tikai tv3.lv``). Feed'ā nekā
no tā nav, bet lapā tas ir katram rakstam, tāpēc lapu ievelkam vienu reizi
un rezultātu kešojam uz raksta (``raw_json["_page_meta"]``).

Kāpēc tas ir vērtīgi:

* **Post ID** dod tv3.lv pašu īso saiti ``tv3.lv/p/3879950`` — tikpat gara
  kā mūsu ``/r/`` kods, bet strādā jau šodien, bez nginx noteikuma.
* **Autors** ļauj ierakstā pieminēt žurnālistu (un redaktoram pārbaudīt,
  kurš raksts kam pieder).
* **Tagi** ir redakcijas pašas atslēgvārdi — labākais hashtag avots nekā
  AI izdomāti.
* **Post type** pasaka, ka rakstā IR video vai galerija, arī tad, kad feed
  to nepiemin: tas tieši nosaka, vai reel/photo_album vispār ir uz galda.
* **Label** ("Tikai tv3.lv") atzīmē ekskluzīvu saturu, ko ir vērts stumt.
* **Content length** atšķir īsziņu no gara lasāmgabala (karuselis/skaidrojums).

Viss ir defensīvs: ja lapa neatbild vai izskatās citādi, atgriežas tukšs
dict un viss pārējais strādā tieši tāpat kā līdz šim.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from urllib.parse import urlparse

from app.models import utcnow

log = logging.getLogger(__name__)

# Neizdevušos ievilkšanu neatkārtojam katrā ciklā — portāls var būt nost,
# bots nobloķēts, raksts aiz maksas sienas.
RETRY_HOURS = 6

# dataLayer atslēgas tā, kā tās raksta tv3.lv GTM slānis.
_KEYS = {
    "post_id": ("Post ID", "PostID", "post_id", "postId"),
    "author": ("Editor name", "Author", "author", "Editor"),
    "tags": ("Tags", "tags", "Tag"),
    "label": ("Label", "label"),
    "post_type": ("Post type", "PostType", "post_type"),
    "page_type": ("Page type", "PageType", "page_type"),
    "content_chars": ("Content length", "ContentLength", "content_length"),
    "publish_date": ("Publish date", "PublishDate", "publish_date"),
    "source": ("Source", "source"),
    "secondary_category": ("Secondary category", "SecondaryCategory"),
}
_CATEGORY_KEYS = tuple(f"Category level {i}" for i in range(1, 6))

_SPLIT_RE = re.compile(r"\s*[;|]\s*")


def _json_object_at(text: str, start: int) -> str:
    """Teksta gabals no `{` līdz tā pārim, ignorējot iekavas stringos."""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


def _candidate_objects(html: str) -> list[str]:
    """Visi dataLayer objekti lapā, spēcīgākais (dlEvent) pirmais."""
    out: list[str] = []
    for marker in ("dlEvent", "dataLayer.push(", "dataLayer = [", "dataLayer=["):
        for m in re.finditer(re.escape(marker), html):
            brace = html.find("{", m.end())
            if brace < 0 or brace - m.end() > 40:
                continue  # `{` par tālu — tas vairs nav šī piešķīruma objekts
            blob = _json_object_at(html, brace)
            if blob:
                out.append(blob)
    return out


def _scrape_pairs(blob: str) -> dict:
    """Atslēga -> vērtība no JS objekta, kas nav derīgs JSON.

    Mums nevajag izpildīt JS: vajag pāris zināmas atslēgas, un tās vienmēr
    ir citātos, jo tajās ir atstarpes ("Post ID", "Editor name").
    """
    pairs: dict[str, object] = {}
    for m in re.finditer(r'"([^"\\]{1,40})"\s*:\s*(?:"((?:[^"\\]|\\.)*)"|(-?\d+))', blob):
        key, text, number = m.group(1), m.group(2), m.group(3)
        if key in pairs:
            continue
        pairs[key] = int(number) if number is not None else _unescape(text)
    return pairs


def _unescape(text: str) -> str:
    try:
        return json.loads(f'"{text}"')
    except ValueError:
        return text


def _pick(data: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = data.get(key)
        if value not in (None, "", []):
            return str(value).strip()
    return ""


def _split(value: str) -> list[str]:
    """"Bauskas iela;Rīga" -> ["Bauskas iela", "Rīga"]."""
    return [p.strip() for p in _SPLIT_RE.split(value or "") if p.strip()]


def _meta_fallbacks(html: str) -> dict:
    """Autors un Post ID no parastā HTML, kad dataLayer nav atrodams."""
    found: dict[str, str] = {}
    for pattern, field in (
        (r'<meta[^>]+property=["\']article:author["\'][^>]+content=["\']([^"\']+)', "author"),
        (r'<meta[^>]+name=["\']author["\'][^>]+content=["\']([^"\']+)', "author"),
        (r'<link[^>]+rel=["\']shortlink["\'][^>]+href=["\'][^"\']*[?/]p[=/](\d+)', "post_id"),
        (r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)',
         "publish_date"),
    ):
        m = re.search(pattern, html, re.I)
        if m and field not in found:
            found[field] = m.group(1).strip()
    return found


def parse(html: str) -> dict:
    """Normalizēti metadati no raksta lapas HTML ({} ja nekā nav)."""
    data: dict = {}
    for blob in _candidate_objects(html or ""):
        try:
            parsed = json.loads(blob)
        except ValueError:
            parsed = _scrape_pairs(blob)
        if not isinstance(parsed, dict):
            continue
        # Vairāki dataLayer push'i: katrs var nest citu gabalu, tāpēc
        # savācam kopā, un pirmais (dlEvent) uzvar konfliktos.
        for key, value in parsed.items():
            data.setdefault(key, value)
    fallbacks = _meta_fallbacks(html or "")

    post_id = _pick(data, _KEYS["post_id"]) or fallbacks.get("post_id", "")
    post_id = post_id if post_id.isdigit() else ""
    categories = [c for c in (_pick(data, (key,)) for key in _CATEGORY_KEYS) if c]
    secondary = _pick(data, _KEYS["secondary_category"])
    if secondary and secondary not in categories:
        categories.append(secondary)
    chars = _pick(data, _KEYS["content_chars"])

    meta = {
        "post_id": post_id,
        "author": _pick(data, _KEYS["author"]) or fallbacks.get("author", ""),
        "tags": _split(_pick(data, _KEYS["tags"])),
        "categories": categories,
        "post_types": [t.lower() for t in _split(_pick(data, _KEYS["post_type"]))],
        "label": _pick(data, _KEYS["label"]),
        "content_chars": int(chars) if chars.isdigit() else 0,
        "publish_date": (_pick(data, _KEYS["publish_date"])
                         or fallbacks.get("publish_date", "")),
        "page_type": _pick(data, _KEYS["page_type"]),
        "source": _pick(data, _KEYS["source"]),
    }
    return meta if any(meta.values()) else {}


def fetch(url: str, timeout: int = 10) -> str:
    """Raksta lapas HTML ('' pie jebkuras kļūdas)."""
    if not (url or "").startswith("http"):
        return ""
    import httpx

    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True,
                         headers={"User-Agent": "TV3-Social-Autopilot/1.0"})
        if resp.status_code != 200:
            log.debug("page meta %s -> HTTP %s", url, resp.status_code)
            return ""
        return resp.text
    except Exception as e:  # noqa: BLE001 — metadati ir bonuss, nevis prasība
        log.debug("page meta fetch failed for %s: %s", url, e)
        return ""


def enabled(rules: dict | None = None) -> bool:
    from app import config

    rules = config.load_rules() if rules is None else rules
    return bool((rules or {}).get("page_meta", True))


def meta(article) -> dict:
    """Kešotie metadati bez tīkla pieprasījuma ({} ja vēl nav ievilkti)."""
    return dict((article.raw_json or {}).get("_page_meta") or {})


def _due(article) -> bool:
    raw = article.raw_json or {}
    if raw.get("_page_meta"):
        return False  # jau ir; metadati praktiski nemainās
    stamp = raw.get("_page_meta_at")
    if not stamp:
        return True
    try:
        last = datetime.fromisoformat(str(stamp))
    except ValueError:
        return True
    return utcnow() - last >= timedelta(hours=RETRY_HOURS)


def enrich(article, force: bool = False, rules: dict | None = None) -> dict:
    """Ievelk lapu vienreiz un nokešo metadatus uz raksta.

    Atgriež metadatus (arī tad, ja tie nāk no keša). Nekad nemet kļūdu:
    portāls var neatbildēt, un tas nedrīkst apturēt lēmumu ciklu.
    """
    if not enabled(rules):
        return meta(article)
    if not (force or _due(article)):
        return meta(article)
    html = fetch(article.canonical_url or article.url)
    found = parse(html) if html else {}
    raw = dict(article.raw_json or {})
    raw["_page_meta_at"] = utcnow().isoformat(timespec="seconds")
    if found:
        raw["_page_meta"] = found
    article.raw_json = raw
    return found or meta(article)


def backfill(session, limit: int = 10, rules: dict | None = None) -> int:
    """Ievelk metadatus rakstiem, kuriem to vēl nav. Atgriež ievilkto skaitu.

    Lēmumu ciklā metadatus dabū tikai vēl neizlemtie raksti; franšīzes
    (QUIZ, ICYMI, evergreen) strādā ar vecākiem, un tie citādi paliktu bez
    autora un tagiem uz visiem laikiem. Vienā ciklā ne vairāk par `limit`
    lapām, lai portālam nesanāktu pieprasījumu vilnis.
    """
    from sqlalchemy import select

    from app import config
    from app.models import Article

    rules = config.load_rules() if rules is None else rules
    if not enabled(rules):
        return 0
    rows = session.execute(
        select(Article).order_by(Article.first_seen_at.desc()).limit(limit * 6)
    ).scalars().all()
    done = 0
    for article in rows:
        if done >= limit:
            break
        if not _due(article):
            continue
        enrich(article, rules=rules)
        done += 1
    return done


# ---------------------------------------------------------------- lasītāji

def post_id(article) -> str:
    return str(meta(article).get("post_id") or "")


def short_url(article) -> str:
    """tv3.lv pašu īsā saite uz rakstu ('' ja Post ID nav zināms)."""
    pid = post_id(article)
    if not pid:
        return ""
    host = urlparse(article.canonical_url or article.url or "").netloc
    host = (host or "tv3.lv").removeprefix("www.")
    return f"https://{host}/p/{pid}"


def tracked_short_url(article, tracked_url: str) -> str:
    """/p/<id> ar to pašu UTM asti, kas bija garajā saitē.

    Īsa saite nedrīkst maksāt mērījumu: parametri tiek pārnesti viens pret
    vienu, tāpēc GA4 redz tieši to pašu, ko redzētu no pilnās saites —
    ar nosacījumu, ka tv3.lv /p/ novirze vaicājuma virkni saglabā.
    """
    base = short_url(article)
    if not base:
        return ""
    query = urlparse(tracked_url or "").query
    return f"{base}?{query}" if query else base


def author(article) -> str:
    return str(meta(article).get("author") or "")


def tags(article, limit: int = 6) -> list[str]:
    return [str(t) for t in (meta(article).get("tags") or [])][:limit]


_TAG_WORD_RE = re.compile(r"[^0-9A-Za-zĀ-ž]+")


def hashtags(article, limit: int = 2) -> list[str]:
    """Redakcijas tagi kā hashtagi ("Gāzes sprādziens" -> "GāzesSprādziens").

    Šos atslēgvārdus rakstam ir uzlicis cilvēks redakcijā, un portāls pats
    ar tiem strādā — precīzāk un konsekventāk nekā jebkas, ko modelis
    izdomā uz vietas. Vairāk par trim vārdiem garš tags kā hashtags vairs
    nelasās, tāpēc tādus izlaižam.
    """
    out: list[str] = []
    for tag in meta(article).get("tags") or []:
        words = [w for w in _TAG_WORD_RE.split(str(tag)) if w]
        if not words or len(words) > 3:
            continue
        text = "".join(w[:1].upper() + w[1:] for w in words)
        if 3 <= len(text) <= 24 and text not in out:
            out.append(text)
    return out[:limit]


def label(article) -> str:
    return str(meta(article).get("label") or "")


def content_chars(article) -> int:
    try:
        return int(meta(article).get("content_chars") or 0)
    except (TypeError, ValueError):
        return 0


def has_video(article) -> bool:
    return "video" in (meta(article).get("post_types") or [])


def has_gallery(article) -> bool:
    return "gallery" in (meta(article).get("post_types") or [])


def is_exclusive(article) -> bool:
    """Redakcijas ekskluzīvs ("Tikai tv3.lv") — saturs, kas ir tikai mums."""
    return "tikai tv3" in label(article).lower()


def video_hint(article) -> str:
    """Ko AI jāzina par video: īstais klips, tikai norāde vai nekas.

    Divi dažādi signāli: feed'a video URL nozīmē, ka reel var būvēt no
    ĪSTĀ klipa; CMS "Post type: Video" pasaka tikai to, ka video rakstā ir —
    saites nav, tāpēc reel tad iznāk kā slideshow. Sajaukt tos nozīmētu
    solīt AI klipu, kura nav.
    """
    from app import reels

    if reels.article_video(article):
        return "ir 9:16 videoklips — reel var būvēt no īstā klipa"
    if has_video(article):
        return ("rakstā ir video, bet klipa saite feed'ā nav — reel tad "
                "jāveido kā slideshow")
    return "nav"


def prompt_lines(article) -> str:
    """CMS metadatu bloks AI promptam ('' kad metadatu nav)."""
    data = meta(article)
    if not data:
        return ""
    lines = []
    if data.get("author"):
        lines.append(f"Autors: {data['author']}")
    if data.get("tags"):
        lines.append("Redakcijas tagi (labākais hashtag avots): "
                     + ", ".join(str(t) for t in data["tags"][:8]))
    if data.get("categories"):
        lines.append("CMS kategorijas: " + ", ".join(str(c) for c in data["categories"]))
    if has_gallery(article):
        lines.append("Rakstā ir foto galerija — der photo_album/karuselim")
    if data.get("content_chars"):
        chars = int(data["content_chars"])
        kind = "gars lasāmgabals" if chars >= 6000 else (
            "vidēja garuma raksts" if chars >= 2500 else "īsziņa")
        lines.append(f"Apjoms: {chars} zīmes ({kind})")
    if is_exclusive(article):
        lines.append(f"Zīmols: {data.get('label')} — ekskluzīvs saturs, "
                     "to ir vērts izcelt")
    return "\n".join(lines)
