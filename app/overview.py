"""Helicopter view: content -> distribution -> advertising -> economics.

One page that answers the marketing director's questions: how much content
came in and how much of it we used, where it went out, what every channel's
session costs, and what the AI would change. External spend (the agency-run
Google Search / Performance Max campaigns) enters as a monthly figure the
admin types in, so the cost-per-session comparison works before any Google
Ads API connection exists — GA4 already counts those sessions in the
Paid Search / Cross-network channel groups.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import func, select

from app.models import (AdEntry, Article, Evaluation, Post, get_setting,
                        set_setting, utcnow)

log = logging.getLogger(__name__)

DAYS = 30

# GA4 default channel groups that carry the externally bought Google traffic
GOOGLE_PAID_CHANNELS = ("Paid Search", "Cross-network", "Paid Shopping",
                        "Display")
META_PAID_CHANNELS = ("Paid Social",)
ORGANIC_SOCIAL_CHANNELS = ("Organic Social",)


def external_spend(session) -> float:
    """Monthly € the business spends on Google campaigns outside this
    system (admin-entered until the Google Ads account is connected)."""
    return float(get_setting(session, "overview:google_monthly_spend", "0") or 0)


def save_external_spend(session, monthly_eur: float) -> None:
    set_setting(session, "overview:google_monthly_spend",
                str(max(0.0, monthly_eur)))


def content_funnel(session, days: int = 7) -> dict:
    """Articles in -> decided -> published posts; the utilization number."""
    since = utcnow() - timedelta(days=days)
    total = session.execute(
        select(func.count(Article.id)).where(Article.first_seen_at >= since)
    ).scalar() or 0
    decided = session.execute(
        select(func.count(Article.id)).where(Article.first_seen_at >= since,
                                             Article.decided_at.is_not(None))
    ).scalar() or 0
    published_articles = session.execute(
        select(func.count(func.distinct(Post.article_id)))
        .where(Post.state == "published", Post.published_at >= since)
    ).scalar() or 0
    posts = session.execute(
        select(Post.channel, Post.format, func.count(Post.id))
        .where(Post.state == "published", Post.published_at >= since)
        .group_by(Post.channel, Post.format)
    ).all()
    by_channel: dict[str, int] = {}
    by_format: dict[str, int] = {}
    for channel, fmt, n in posts:
        by_channel[channel] = by_channel.get(channel, 0) + n
        by_format[fmt] = by_format.get(fmt, 0) + n
    return {
        "days": days, "articles": total, "decided": decided,
        "published_articles": published_articles,
        "posts": sum(by_channel.values()),
        "by_channel": dict(sorted(by_channel.items(), key=lambda kv: -kv[1])),
        "by_format": dict(sorted(by_format.items(), key=lambda kv: -kv[1])),
        "utilization": (published_articles / total * 100) if total else 0.0,
    }


def our_ads_summary(session, days: int = DAYS) -> dict:
    since = utcnow() - timedelta(days=days)
    rows = session.execute(
        select(AdEntry).where(AdEntry.updated_at >= since,
                              AdEntry.status.in_(("active", "paused", "done")))
    ).scalars().all()
    spend = sum(e.spent_cents for e in rows) / 100
    sessions = sum(e.sessions for e in rows)
    clicks = sum(e.clicks for e in rows)
    return {"n": len(rows), "spend": spend, "sessions": sessions,
            "clicks": clicks,
            "cps": (spend / sessions) if sessions else None}


def channel_economics(session, days: int = DAYS) -> dict:
    """Cost per session per money bucket: the agency's Google campaigns,
    our Meta ads, and organic social as the free baseline."""
    from app import ga4

    channels = ga4.channel_economics(days)
    def _bucket(names):
        rows = [c for c in channels if c["channel"] in names]
        return {"sessions": sum(c["sessions"] for c in rows),
                "engaged": sum(c["engaged"] for c in rows)}

    google = _bucket(GOOGLE_PAID_CHANNELS)
    meta_paid = _bucket(META_PAID_CHANNELS)
    organic = _bucket(ORGANIC_SOCIAL_CHANNELS)
    google_spend = external_spend(session) * days / 30.4
    ours = our_ads_summary(session, days)
    return {
        "configured": bool(channels),
        "channels": channels,
        "google": {**google, "spend": round(google_spend, 2),
                   "cps": (google_spend / google["sessions"])
                   if google["sessions"] else None},
        "meta_paid": {**meta_paid, "spend": ours["spend"],
                      "cps": (ours["spend"] / meta_paid["sessions"])
                      if meta_paid["sessions"] else ours["cps"]},
        "organic_social": organic,
        "our_ads": ours,
    }


# Iemeslu grupas «kāpēc raksts nepublicējās». Neapstrādāti reason teksti ir
# pārāk sīki (katrs ar savu stundu skaitu), tāpēc grupējam pēc atslēgvārda.
REASON_GROUPS = (
    ("editor status: don't", "Redaktors atzīmēja «nepublicēt»"),
    ("too old", "Raksts par vecu kanāla svaiguma limitam"),
    ("not routed", "Sadaļa netiek raidīta uz šo kanālu"),
    ("blocklist", "Term ID kanāla melnajā sarakstā"),
    ("allowlist", "Term ID nav kanāla baltajā sarakstā"),
    ("nav derīga laika", "Rinda bija pilna — nav derīga laika slota"),
    ("atkārtojums tajā pašā formātā", "Otrais vilnis tajā pašā formātā"),
    ("story needs an image", "Stāstam trūkst attēla vai renderētāja"),
    ("sensitivity", "Jutīga tēma pēc noteikumiem"),
)


def _reason_label(reason: str) -> str:
    low = (reason or "").lower()
    for needle, label in REASON_GROUPS:
        if needle.lower() in low:
            return label
    return (reason or "cits iemesls")[:80]


def publication_funnel(session, days: int = 7) -> dict:
    """Kāpēc daļa rakstu nepublicējas: katrs perioda raksts vienā kastē.

    Skaitītājs «publicēti raksti / raksti» pats par sevi maldina — saucējā ir
    arī raksti, kas tikko ienāca un vēl gaida savu rindu. Šis sadalījums
    parāda, cik daudz ir īsts atteikums un cik — vēl neizšķirts."""
    since = utcnow() - timedelta(days=days)
    articles = session.execute(
        select(Article).where(Article.first_seen_at >= since)
    ).scalars().all()
    if not articles:
        return {"days": days, "total": 0, "buckets": [], "reasons": []}
    ids = [a.id for a in articles]
    states: dict[int, set[str]] = {}
    for aid, state in session.execute(
            select(Post.article_id, Post.state).where(Post.article_id.in_(ids))):
        states.setdefault(aid, set()).add(state)
    evals: dict[int, list[tuple[str, str]]] = {}
    for aid, outcome, reason in session.execute(
            select(Evaluation.article_id, Evaluation.outcome, Evaluation.reason)
            .where(Evaluation.article_id.in_(ids))):
        evals.setdefault(aid, []).append((outcome, reason))

    buckets: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for a in articles:
        st = states.get(a.id, set())
        outcomes = evals.get(a.id, [])
        if "published" in st:
            key = "Publicēts"
        elif st & {"scheduled", "publishing", "proposed"}:
            key = "Vēl rindā (ieplānots)"
        elif st & {"failed"}:
            key = "Publicēšana neizdevās"
        elif a.editor_status == "dont":
            key = "Redaktors atzīmēja «nepublicēt»"
        elif a.decided_at is None:
            key = "Vēl nav izvērtēts"
        elif any(o == "ai_skip" for o, _ in outcomes):
            key = "AI izlēma nepublicēt"
            for o, r in outcomes:
                if o == "ai_skip" and r:
                    reasons[_reason_label(r)] = reasons.get(_reason_label(r), 0) + 1
                    break
        elif any(o == "blocked" for o, _ in outcomes):
            key = "Noteikumi bloķēja"
            for o, r in outcomes:
                if o == "blocked":
                    reasons[_reason_label(r)] = reasons.get(_reason_label(r), 0) + 1
                    break
        elif "cancelled" in st:
            key = "Atcelts"
        else:
            key = "Cits"
        buckets[key] = buckets.get(key, 0) + 1
    total = len(articles)
    order = ["Publicēts", "Vēl rindā (ieplānots)", "Vēl nav izvērtēts",
             "AI izlēma nepublicēt", "Noteikumi bloķēja",
             "Redaktors atzīmēja «nepublicēt»", "Publicēšana neizdevās",
             "Atcelts", "Cits"]
    rows = [{"label": k, "n": buckets[k], "pct": buckets[k] / total * 100}
            for k in order if k in buckets]
    return {"days": days, "total": total, "buckets": rows,
            "reasons": sorted(({"label": k, "n": v} for k, v in reasons.items()),
                              key=lambda r: -r["n"])[:6]}


FRANCHISE_LABELS = {
    "mondaytop5": "Pr · Nogales TOP 5",
    "mondaystory": "Pr · Nogales stāsts",
    "number": "Ot · Nedēļas skaitlis",
    "question": "Tr · Trešdienas jautājums",
    "yearago": "Ce · Šajā dienā pirms gada",
    "guide": "Pk · Nogales gids",
    "digest": "Se/Sv · Nedēļas TOP 5",
    "digestreel": "Se · Nedēļa 30 sekundēs",
    "icymi": "Se · Nepamanītais stāsts",
    "evergreen": "Sv · Arhīva raksts",
    "quiz": "Sv · Nedēļas QUIZ",
    "dailystory": "Pr–Pk · Dienas TOP 3",
}


def franchise_stats(session, days: int = 28) -> dict:
    """Franšīžu «kill / keep» tabula: katra nosauktā formāta vidējās GA4
    sesijas uz ierakstu pret parasto ierakstu vidējo tajā pašā kanālā.

    Četru nedēļu logs ir apzināts — tik ilgi vajag, lai formāts sāktu
    uzkrāt atgriezenisko auditoriju; ātrāks spriedums nogalinātu formātu,
    kas tikai vēl nav pamanīts. Verdikts «vāji» nav pavēle izslēgt: stāstu
    un gida uzdevums ir zīmols un sasniedzamība, ne klikšķi."""
    from app.models import PostMetrics

    since = utcnow() - timedelta(days=days)
    rows = session.execute(
        select(Post.hook_type, func.count(func.distinct(Post.id)),
               func.coalesce(func.sum(PostMetrics.ga_sessions), 0))
        .outerjoin(PostMetrics, PostMetrics.post_id == Post.id)
        .where(Post.state == "published", Post.published_at >= since)
        .group_by(Post.hook_type)
    ).all()
    baseline_posts = baseline_sessions = 0
    items = []
    for hook, n, sessions in rows:
        if hook in FRANCHISE_LABELS:
            items.append({"hook": hook, "label": FRANCHISE_LABELS[hook],
                          "posts": n, "sessions": int(sessions or 0),
                          "per_post": (sessions or 0) / n if n else 0.0})
        else:   # parastie redakcijas ieraksti = bāzes līnija
            baseline_posts += n
            baseline_sessions += int(sessions or 0)
    benchmark = (baseline_sessions / baseline_posts) if baseline_posts else 0.0
    for it in items:
        it["vs_benchmark"] = (it["per_post"] / benchmark) if benchmark else 0.0
        if it["posts"] < 3:
            it["verdict"] = "par agru"
        elif not benchmark or it["vs_benchmark"] >= 1.0:
            it["verdict"] = "turēt"
        elif it["vs_benchmark"] >= 0.6:
            it["verdict"] = "vērot"
        else:
            it["verdict"] = "vāji"
    items.sort(key=lambda it: -it["per_post"])
    return {"days": days, "benchmark": benchmark,
            "baseline_posts": baseline_posts, "items": items}


def build(session) -> dict:
    return {
        "funnel7": content_funnel(session, 7),
        "funnel1": content_funnel(session, 1),
        "economics": channel_economics(session),
        "franchises": franchise_stats(session),
        "publication": publication_funnel(session, 7),
        "google_monthly": external_spend(session),
        "ai_report": get_setting(session, "overview:ai_report", ""),
        "ai_report_at": get_setting(session, "overview:ai_report_at", ""),
    }


def weekly_ai_report(session) -> None:
    """Monday cron: refresh the marketer memo so Pārskats opens the week
    with current recommendations; forwarded to Slack when configured."""
    from app import credentials
    from app.pipeline import alert

    if not credentials.get("anthropic_api_key", session):
        return  # bez atslēgas nav ko ģenerēt — lapa rāda norādi pati
    text = ai_report(session)
    if text and not text.startswith("Ieteikumu ģenerēšana neizdevās"):
        alert("TV3 Autopilot — AI mārketinga ieteikumi (pirmdienas apskats):\n"
              + text)


def ai_report(session) -> str:
    """The performance-marketer memo: Claude reads the same numbers the page
    shows and writes 3-5 concrete recommendations. Cached until regenerated."""
    from app import config, credentials

    api_key = credentials.get("anthropic_api_key", session)
    if not api_key:
        return ("AI atslēga nav pieslēgta (Konti → Anthropic) — ieteikumi "
                "nav pieejami.")
    data = build(session)
    eco = data["economics"]

    def fmt_cps(v):
        return f"{v:.2f} €/sesija" if v else "nav datu"

    context = f"""Saturs (7 d): {data['funnel7']['articles']} raksti, publicēti
{data['funnel7']['published_articles']} ({data['funnel7']['utilization']:.0f}%),
{data['funnel7']['posts']} ieraksti pa kanāliem {data['funnel7']['by_channel']}.
Kanālu ekonomika (30 d):
- Google maksas (aģentūras Search/PMax): {eco['google']['sessions']} sesijas,
  tēriņš ~{eco['google']['spend']:.0f} €, {fmt_cps(eco['google']['cps'])}
- Mūsu Meta reklāmas: {eco['our_ads']['n']} reklāmas, tēriņš
  {eco['our_ads']['spend']:.2f} €, {eco['our_ads']['sessions']} sesijas,
  {fmt_cps(eco['our_ads']['cps'])}
- Organic Social: {eco['organic_social']['sessions']} sesijas (0 €)
Visi GA4 kanāli: {[(c['channel'], c['sessions']) for c in eco['channels'][:8]]}
Satura franšīzes (28 d; vidējais parastam ierakstam
{data['franchises']['benchmark']:.0f} sesijas): {[(i['label'], i['posts'],
round(i['per_post']), i['verdict']) for i in data['franchises']['items']]}"""
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=config.AI_MODEL_STRONG, max_tokens=900,
            system=("Tu esi pieredzējis performance mārketinga vadītājs ziņu "
                    "medijā. Mērķis: ar mazāku budžetu vairāk sesiju uz tv3.lv. "
                    "Atbildi latviski, nevainojamā pareizrakstībā, bez ūdens."),
            messages=[{"role": "user", "content":
                f"{context}\n\nUzraksti 3-5 konkrētus ieteikumus budžeta un "
                f"satura izplatīšanas uzlabošanai. Katrs: ko darīt, kāpēc "
                f"(ar skaitli no datiem), gaidāmais efekts. Ja kādam kanālam "
                f"trūkst datu, pasaki, kas jāpieslēdz, lai to izmērītu. "
                f"Vienā punktā izvērtē satura franšīzes: kuras turēt, kuras "
                f"izslēgt, kuras vēl par agru vērtēt."}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        set_setting(session, "overview:ai_report", text)
        set_setting(session, "overview:ai_report_at",
                    utcnow().strftime("%Y-%m-%d %H:%M"))
        return text
    except Exception as e:  # noqa: BLE001
        log.warning("overview AI report failed: %s", e)
        return f"Ieteikumu ģenerēšana neizdevās: {e}"
