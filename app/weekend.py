"""Nedēļas nogales otrreizējie formāti: redakcija brīvdienās raksta mazāk,
tāpēc sistēma no jau izmērītā satura uzbūvē svaigus ierakstus.

Pieci formāti, katrs ar savu slēdzi (Pārskata lapā):
  top5       — «Nedēļas TOP 5» karuselis (sestdien sports, svētdien kopējais)
  reel       — «Nedēļa 90 sekundēs» slaidrādes reels no TOP virsrakstiem
  icymi      — «Nedēļas nepamanītais stāsts»: labs raksts, kam pirmajā reizē
               klājās vāji, saņem vienu atkārtojumu ar citu leņķi
  quiz       — nedēļas kvīza karuselis (jautājums kartītē, atbilde rakstā)
  evergreen  — arhīva raksts, ko joprojām lasa, svētdienas rītā

Valodas disciplīna: visos tekstos datumi ir absolūti («26. augustā»), nekad
relatīvi («vakar», «šonedēļ» ir pieļaujams tikai publicēšanas nedēļā pašā
ierakstā, tāpēc arī to nelietojam) — vecs ieraksts, ko Facebook izceļ pēc
mēneša, nedrīkst maldināt. Grafikās datums ir iededzināts (cards.date_chip).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app import config
from app.models import Article, Post, PostMetrics, get_setting, set_setting, utcnow

log = logging.getLogger(__name__)

FEATURES = ("top5", "reel", "icymi", "quiz", "evergreen")
CHANNEL = "fb_tv3lv"

# Latviešu datumu vārdi — lokatīvs («26. augustā»)
MONTHS_LOC = ["", "janvārī", "februārī", "martā", "aprīlī", "maijā", "jūnijā",
              "jūlijā", "augustā", "septembrī", "oktobrī", "novembrī", "decembrī"]

# Relatīvie laika vārdi, kas novecojušā ierakstā melo — AI ģenerētos tekstos
# tos aizstājam ar absolūto datumu vai ierakstu atmetam.
RELATIVE_WORDS = ("vakar", "šodien", "šorīt", "šovakar", "rīt", "parīt",
                  "aizvakar", "šonedēļ", "pagājušonedēļ", "nupat", "tikko")


def lv_date(dt: datetime) -> str:
    """«26. augustā» — bez gada, ja tas ir šis gads."""
    base = f"{dt.day}. {MONTHS_LOC[dt.month]}"
    return base if dt.year == utcnow().year else f"{base} ({dt.year}. gads)"


def has_relative_words(text: str) -> str:
    low = f" {text.lower()} "
    for w in RELATIVE_WORDS:
        if f" {w}" in low:
            return w
    return ""


# --- slēdži ---------------------------------------------------------------

def settings(session) -> dict[str, bool]:
    return {f: get_setting(session, f"weekend:{f}", "on") == "on"
            for f in FEATURES}


def save_settings(session, enabled: dict[str, bool]) -> None:
    for f in FEATURES:
        set_setting(session, f"weekend:{f}", "on" if enabled.get(f) else "off")


def _ran_key(feature: str, day) -> str:
    return f"weekend:ran:{feature}:{day.isoformat()}"


# --- nedēļas dati ---------------------------------------------------------

def week_top(session, section: str | None = None, days: int = 7,
             limit: int = 5, max_age_days: int | None = None) -> list[Article]:
    """Raksti ar visvairāk izmērītajām sesijām (GA4 caur post_metrics);
    kamēr metriku nav, krīt atpakaļ uz AI vērtējumu."""
    since = utcnow() - timedelta(days=days)
    q = (select(Article,
                func.coalesce(func.sum(PostMetrics.ga_sessions), 0).label("s"),
                func.coalesce(func.sum(PostMetrics.clicks), 0).label("c"))
         .join(Post, Post.article_id == Article.id)
         .outerjoin(PostMetrics, PostMetrics.post_id == Post.id)
         .where(Post.state == "published", Post.published_at >= since,
                Article.title != "")
         .group_by(Article.id))
    if section:
        q = q.where(Article.section == section)
    if max_age_days:
        q = q.where(Article.published_at
                    >= utcnow() - timedelta(days=max_age_days))
    rows = session.execute(q).all()
    scored = sorted(rows, key=lambda r: (-(r.s or 0), -(r.c or 0),
                                         -(r.Article.ai_score or 0)))
    seen, out = set(), []
    for r in scored:
        if r.Article.id in seen:
            continue
        seen.add(r.Article.id)
        out.append(r.Article)
        if len(out) >= limit:
            break
    return out


def _digest_article(session, guid: str, title: str, section: str,
                    url: str) -> Article:
    """Sintētisks raksts digest ierakstam (Post prasa article_id)."""
    existing = session.execute(
        select(Article).where(Article.guid == guid)).scalars().first()
    if existing:
        return existing
    a = Article(guid=guid, url=url, canonical_url=url, title=title,
                section=section, editor_status="can", decided_at=utcnow(),
                published_at=utcnow(), raw_json={"_digest": True})
    session.add(a)
    session.flush()
    return a


def _schedule(session, article: Article, fmt: str, copy: str, media: list,
              link: str, marker: str, at: datetime,
              card_links: list[str] | None = None) -> Post:
    from app import runtime

    post = Post(article_id=article.id, channel=CHANNEL, format=fmt, copy=copy,
                media=media, link_url=link, hook_type=marker,
                state="scheduled", scheduled_at=at,
                extra={"card_links": card_links} if card_links else {},
                dry_run=runtime.is_dry_run(session))
    session.add(post)
    session.flush()
    return post


def _local_slot(day, hour: int) -> datetime:
    tz = ZoneInfo(config.TIMEZONE)
    local = datetime(day.year, day.month, day.day, hour, 0,
                     tzinfo=tz)
    return local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def _point_line(i: int, article: Article) -> str:
    dt = article.published_at or article.first_seen_at
    date = lv_date(dt) if dt else ""
    title = article.title.rstrip(".")
    return f"{title} ({date})" if date else title


# --- formāti --------------------------------------------------------------

def build_top5(session, day, section: str | None) -> Post | None:
    from app import cards

    articles = week_top(session, section=section)
    if len(articles) < 3 or not cards.renderer_available():
        return None
    sec = section or "news"
    label = {"sport": "Nedēļas sports", None: "Nedēļas TOP",
             "news": "Nedēļas TOP"}.get(section, "Nedēļas TOP")
    title = f"{label}: 5 svarīgākie notikumi"
    points = [_point_line(i, a) for i, a in enumerate(articles, 1)][:5]
    image = next((img for a in articles for img in (a.images or []) if img), "")
    try:
        media = cards.render_cards(
            title, sec, "#TOP5", points[:4], image,
            "Visi stāsti — tv3.lv", date_txt=day.strftime("%d.%m.%Y"))
    except Exception as e:  # noqa: BLE001
        log.warning("top5 render failed: %s", e)
        return None
    link = ("https://tv3.lv/sports" if section == "sport" else "https://tv3.lv")
    week_from = lv_date(utcnow() - timedelta(days=6))
    week_to = lv_date(utcnow())
    copy = (f"{label} — pieci notikumi, par kuriem visvairāk lasīja tv3.lv "
            f"({week_from} – {week_to}). Pilnie stāsti portālā.")
    # kartīte -> SAVS raksts: media ir [vāks, punkti..., CTA kartīte], tāpēc
    # saites sarindojam tāpat — vāks un CTA ved uz sadaļu, punkti uz rakstiem
    used = articles[:max(0, len(media) - 2)]
    card_links = ([link] + [a.canonical_url or a.url for a in used]
                  + [link])[:len(media)]
    return _schedule(session, _digest_article(
        session, f"digest-top5-{sec}-{day.isoformat()}", title, sec, link),
        "card_carousel", copy, media, link, "digest", _local_slot(day, 10),
        card_links=card_links)


def build_reel_digest(session, day) -> Post | None:
    from app import reels

    articles = week_top(session, limit=5)
    if len(articles) < 3 or not reels.available():
        return None
    points = [_point_line(i, a) for i, a in enumerate(articles, 1)][:5]
    title = "Nedēļa 90 sekundēs"
    try:
        media = [reels.build_reel(title, "news", "", points, max_points=5)]
    except Exception as e:  # noqa: BLE001
        log.warning("reel digest failed: %s", e)
        return None
    copy = ("Nedēļa 90 sekundēs — pieci notikumi, par kuriem runāja Latvija. "
            "Pilnie stāsti portālā tv3.lv.")
    return _schedule(session, _digest_article(
        session, f"digest-reel-{day.isoformat()}", title, "news",
        "https://tv3.lv"), "reel", copy, media, "https://tv3.lv",
        "digestreel", _local_slot(day, 12))


def build_icymi(session, day) -> Post | None:
    """Labs raksts (score >= 0.7) ar vājāko izmērīto atdevi — viena otrā
    iespēja ar godīgu datumu tekstā."""
    since = utcnow() - timedelta(days=7)
    rows = session.execute(
        select(Article, func.coalesce(func.sum(PostMetrics.ga_sessions), 0)
               .label("s"))
        .join(Post, Post.article_id == Article.id)
        .outerjoin(PostMetrics, PostMetrics.post_id == Post.id)
        .where(Post.state == "published", Post.published_at >= since,
               Post.channel == CHANNEL, Article.ai_score >= 0.7)
        .group_by(Article.id)
    ).all()
    if not rows:
        return None
    reposted = {p.article_id for p in session.execute(
        select(Post).where(Post.hook_type.in_(("icymi", "evergreen")))
    ).scalars().all()}
    candidates = [r for r in rows if r.Article.id not in reposted
                  and not (r.Article.raw_json or {}).get("_digest")]
    if not candidates:
        return None
    weakest = min(candidates, key=lambda r: r.s or 0)
    art = weakest.Article
    dt = art.published_at or art.first_seen_at
    copy = (f"Stāsts, ko daudzi palaida garām: {art.title.rstrip('.')}. "
            f"Publicēts {lv_date(dt)} — joprojām vērts izlasīt.")
    return _schedule(session, art, "link", copy, [],
                     art.canonical_url or art.url, "icymi",
                     _local_slot(day, 15))


def build_quiz(session, day) -> Post | None:
    """Kvīza karuselis no nedēļas TOP — jautājumus raksta AI; bez AI atslēgas
    formāts izlaižas (kvīzs bez īstiem jautājumiem nav publicējams)."""
    from app import cards, credentials

    api_key = credentials.get("anthropic_api_key", session)
    articles = week_top(session, limit=5)
    if not api_key or len(articles) < 3 or not cards.renderer_available():
        return None
    facts = "\n".join(f"- {a.title} ({lv_date(a.published_at or a.first_seen_at)})"
                      for a in articles)
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=config.AI_MODEL_STRONG, max_tokens=500,
            messages=[{"role": "user", "content":
                f"No šiem tv3.lv nedēļas virsrakstiem uzraksti 3 īsus kvīza "
                f"jautājumus latviski (katru jaunā rindā, bez numerācijas, "
                f"bez atbildēm). Nevainojama pareizrakstība un galotnes. "
                f"Datumus raksti absolūti (piem., «26. augustā»), nekad "
                f"relatīvi.\n{facts}"}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        questions = [q.strip() for q in text.splitlines() if q.strip()][:3]
    except Exception as e:  # noqa: BLE001
        log.warning("quiz generation failed: %s", e)
        return None
    questions = [q for q in questions if not has_relative_words(q)]
    if len(questions) < 2:
        return None
    title = "Nedēļas kvīzs: vai sekoji notikumiem?"
    try:
        media = cards.render_cards(title, "news", "#KVĪZS", questions,
                                   "", "Atbildes — tv3.lv",
                                   date_txt=day.strftime("%d.%m.%Y"))
    except Exception as e:  # noqa: BLE001
        log.warning("quiz render failed: %s", e)
        return None
    copy = ("Nedēļas kvīzs — trīs jautājumi par notikumiem, par kuriem "
            "rakstīja tv3.lv. Atbildes atradīsi portālā.")
    return _schedule(session, _digest_article(
        session, f"digest-quiz-{day.isoformat()}", title, "news",
        "https://tv3.lv"), "card_carousel", copy, media, "https://tv3.lv",
        "quiz", _local_slot(day, 12))


def build_evergreen(session, day) -> Post | None:
    """Arhīva raksts (vecāks par 14 dienām), ko GA4 joprojām redz lasām."""
    old_enough = utcnow() - timedelta(days=14)
    recent_metrics = utcnow() - timedelta(days=7)
    rows = session.execute(
        select(Article, func.sum(PostMetrics.ga_sessions).label("s"))
        .join(Post, Post.article_id == Article.id)
        .join(PostMetrics, PostMetrics.post_id == Post.id)
        .where(Article.published_at <= old_enough,
               PostMetrics.collected_at >= recent_metrics,
               Article.sensitivity == [])
        .group_by(Article.id)
        .order_by(func.sum(PostMetrics.ga_sessions).desc())
    ).all()
    reposted = {p.article_id for p in session.execute(
        select(Post).where(Post.hook_type.in_(("icymi", "evergreen")))
    ).scalars().all()}
    pick = next((r.Article for r in rows
                 if r.Article.id not in reposted and (r.s or 0) > 0
                 and not (r.Article.raw_json or {}).get("_digest")), None)
    if pick is None:
        return None
    dt = pick.published_at or pick.first_seen_at
    copy = (f"Joprojām aktuāli: {pick.title.rstrip('.')}. "
            f"Publicēts {lv_date(dt)}, un lasītāji pie tā atgriežas "
            f"vēl šobrīd.")
    return _schedule(session, pick, "link", copy, [],
                     pick.canonical_url or pick.url, "evergreen",
                     _local_slot(day, 9))


# --- orķestris ------------------------------------------------------------

def run(session, now: datetime | None = None) -> int:
    """Stundas solis: brīvdienās izpilda ieslēgtos formātus, katru vienreiz
    savā dienā. Sestdiena: sporta TOP 5 + reels + icymi; svētdiena:
    kopējais TOP 5 + kvīzs + evergreen."""
    now = now or utcnow()
    local = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(
        ZoneInfo(config.TIMEZONE))
    if local.weekday() not in (5, 6) or local.hour < 8:
        return 0
    day = local.date()
    plan = ({"top5_sport": lambda: build_top5(session, day, "sport"),
             "reel": lambda: build_reel_digest(session, day),
             "icymi": lambda: build_icymi(session, day)}
            if local.weekday() == 5 else
            {"top5_all": lambda: build_top5(session, day, None),
             "quiz": lambda: build_quiz(session, day),
             "evergreen": lambda: build_evergreen(session, day)})
    toggles = settings(session)
    created = 0
    for name, builder in plan.items():
        feature = name.split("_")[0] if name.startswith("top5") else name
        if not toggles.get(feature, False):
            continue
        if get_setting(session, _ran_key(name, day)):
            continue
        try:
            post = builder()
        except Exception as e:  # noqa: BLE001 — brīvdienu formāti negāž pārējo
            log.warning("weekend %s failed: %s", name, e)
            continue
        set_setting(session, _ran_key(name, day), "done")
        if post is not None:
            created += 1
            log.info("weekend %s scheduled as post %s", name, post.id)
    session.commit()
    return created
