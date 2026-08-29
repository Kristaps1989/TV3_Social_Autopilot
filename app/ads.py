"""Ads autopilot orchestration (Phase 0: dry-run planning).

Every cycle: pick boostable candidates from recently published Facebook
posts, allocate the configured daily budget across them, and record the
plan as AdEntry rows. In dry mode nothing leaves the building — the /ads
page shows exactly what WOULD be boosted and for how much. Later phases
turn the same plan into real campaigns via adapters.meta_ads.

Safety model (in order):
  1. hard veto — TTPA politics/social issues and tragedy/crime are never
     boosted, whatever the AI said (keyword net catches fallback decisions);
  2. AI boostable verdict from the decision layer;
  3. mode gate — off / dry / approve / auto.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select

from app.models import AdEntry, Article, Post, get_setting, set_setting, utcnow

log = logging.getLogger(__name__)

MODES = ("off", "dry", "approve", "auto")
MIN_AD_BUDGET_EUR = 5.0        # zem šī learning phase nekad nesāksies
CANDIDATE_WINDOW_HOURS = 48    # ziņu dzīves cikls: boostojam tikai svaigo
MAX_ACTIVE_ADS = 6             # budžets dalās starp tik daudz reklāmām max

# TTPA drošības tīkls: ja virsrakstā/ievadā ir kāds no šiem celmiem, raksts
# netiek boostots arī tad, ja AI (vai fallback bez AI) neiebilda. Apzināti
# plašs — kļūdīties šeit nozīmē riskēt ar visa konta bloķēšanu.
POLITICS_STEMS = (
    "saeim", "vēlēšan", "referend", "partij", "ministr", "valdīb", "politi",
    "prezident", "deputāt", "koalīcij", "opozīcij", "pašvaldīb", "domes",
    "nato", "karš", "kara ", "iebrukum", "mobilizāc", "sankcij", "migrant",
    "migrāc", "bēgļ", "protest", "streik", "abort", "eitanāzij",
    "parlament", "likumprojekt", "budžeta deficīt",
)


# --- settings -------------------------------------------------------------

def settings(session) -> dict:
    mode = get_setting(session, "ads:mode", "off")
    return {
        "mode": mode if mode in MODES else "off",
        "daily_budget": float(get_setting(session, "ads:daily_budget", "0") or 0),
        "brand_share": int(get_setting(session, "ads:brand_share", "20") or 20),
    }


def save_settings(session, mode: str, daily_budget: float, brand_share: int) -> None:
    set_setting(session, "ads:mode", mode if mode in MODES else "off")
    set_setting(session, "ads:daily_budget", str(max(0.0, daily_budget)))
    set_setting(session, "ads:brand_share", str(min(50, max(0, brand_share))))


# --- boostability ---------------------------------------------------------

def politics_hit(article: Article) -> str:
    text = f"{article.title} {article.lead or ''}".lower()
    for stem in POLITICS_STEMS:
        if stem in text:
            return stem
    return ""


def boostable(article: Article) -> tuple[bool, str]:
    """(drīkst boostot, iemesls). Veto slāņi pirms AI viedokļa."""
    if any(s in ("tragedy", "crime") for s in (article.sensitivity or [])):
        return False, "sensitīvs saturs (traģēdija/noziegums) — nereklamējam"
    hit = politics_hit(article)
    if hit:
        return False, f"politika/sabiedriskie jautājumi (TTPA, “{hit}…”) — Meta ES nepieņem"
    raw = article.raw_json or {}
    if raw.get("_boostable") is False:
        return False, raw.get("_boost_reason") or "AI: nav boostojams"
    if raw.get("_boostable") is True:
        return True, raw.get("_boost_reason") or "AI: drīkst reklamēt"
    # bez AI lēmuma (fallback): sports/izklaide droši, ziņas piesardzīgi nē
    if article.section in ("sport", "entertainment"):
        return True, "sadaļa pēc noklusējuma droša (nav AI vērtējuma)"
    return False, "nav AI boostable vērtējuma — news bez tā neboostojam"


# --- candidate selection and the plan ------------------------------------

BOOSTABLE_FORMATS = ("link", "card_carousel", "photo")


def candidates(session, now=None) -> tuple[list[dict], list[dict]]:
    """(izvēlētie, noraidītie ar iemesliem) no pēdējo 48 h FB ierakstiem."""
    now = now or utcnow()
    since = now - timedelta(hours=CANDIDATE_WINDOW_HOURS)
    rows = session.execute(
        select(Post).where(Post.state == "published",
                           Post.published_at >= since,
                           Post.channel == "fb_tv3lv",
                           Post.format.in_(BOOSTABLE_FORMATS))
        .order_by(Post.published_at.desc())
    ).scalars().all()

    picked: list[dict] = []
    rejected: list[dict] = []
    seen_articles: set[int] = set()
    for post in rows:
        art = post.article
        if art is None or not post.link_url:
            continue
        if art.id in seen_articles:
            continue
        seen_articles.add(art.id)
        ok, reason = boostable(art)
        entry = {"post": post, "article": art, "reason": reason,
                 "score": float(art.ai_score or 0)}
        (picked if ok else rejected).append(entry)
    picked.sort(key=lambda e: -e["score"])
    return picked[:MAX_ACTIVE_ADS], rejected


def build_plan(session, now=None) -> dict:
    """Dienas plāns: kandidāti + budžeta sadale. Tikai aprēķins, bez API."""
    cfg = settings(session)
    picked, rejected = candidates(session, now)
    budget = cfg["daily_budget"]
    brand_eur = round(budget * cfg["brand_share"] / 100, 2)
    perf_eur = round(budget - brand_eur, 2)
    rows = []
    if picked and perf_eur >= MIN_AD_BUDGET_EUR:
        # vienmērīgs sākums; svari pēc rezultātiem ienāk 2. fāzē ar bandit
        n = min(len(picked), max(1, int(perf_eur // MIN_AD_BUDGET_EUR)))
        share = round(perf_eur / n, 2)
        for e in picked[:n]:
            rows.append({**e, "budget_eur": share, "objective": "traffic"})
        for e in picked[n:]:
            rejected.append({**e, "reason": "budžets šodien pilns — rindā"})
    elif picked:
        for e in picked:
            rejected.append({**e, "reason": "dienas budžets par mazu "
                                            f"(min {MIN_AD_BUDGET_EUR:.0f} € reklāmai)"})
    return {"settings": cfg, "planned": rows, "rejected": rejected,
            "brand_eur": brand_eur, "perf_eur": perf_eur}


def sync_entries(session, now=None) -> int:
    """Materialise the current plan as AdEntry rows (dry/approve modes).
    Planned rows are refreshed in place; entries whose post fell out of the
    plan go back to 'rejected' with the newest reason."""
    now = now or utcnow()
    plan = build_plan(session, now)
    by_post = {e.post_id: e for e in session.execute(
        select(AdEntry).where(AdEntry.status.in_(("candidate", "planned")))
    ).scalars().all()}
    changed = 0
    for row in plan["planned"]:
        post = row["post"]
        entry = by_post.pop(post.id, None)
        if entry is None:
            entry = AdEntry(post_id=post.id, article_id=row["article"].id)
            session.add(entry)
        entry.status = "planned"
        entry.objective = row["objective"]
        entry.budget_cents = int(row["budget_eur"] * 100)
        entry.reason = row["reason"]
        entry.updated_at = now
        changed += 1
    for entry in by_post.values():
        entry.status = "rejected"
        entry.reason = "izkrita no dienas plāna"
        entry.updated_at = now
    session.commit()
    return changed


def tick(session) -> None:
    """Hourly scheduler step. Off = nothing at all; dry/approve = refresh the
    plan so /ads always shows the current picture. Live execution (auto)
    arrives with Phase 1 — deliberately absent here."""
    cfg = settings(session)
    if cfg["mode"] == "off":
        return
    try:
        sync_entries(session)
    except Exception as e:  # noqa: BLE001 — ads nekad nedrīkst gāzt publicēšanu
        log.warning("ads tick failed: %s", e)
