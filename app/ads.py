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

    busy_posts = {e.post_id for e in session.execute(
        select(AdEntry).where(AdEntry.status.in_(
            ("awaiting_approval", "active", "paused", "done")))
    ).scalars().all()}
    picked: list[dict] = []
    rejected: list[dict] = []
    seen_articles: set[int] = set()
    for post in rows:
        if post.id in busy_posts:
            continue  # jau reklāmā — budžetu tam tur aktīvais ieraksts
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
    # jau aktīvās reklāmas tērē savu dienas budžetu — jaunie kandidāti dala
    # tikai atlikumu, citādi kopējais tēriņš pārsniegtu dienas limitu
    committed = session.execute(
        select(AdEntry).where(AdEntry.status.in_(("awaiting_approval", "active")))
    ).scalars().all()
    committed_eur = round(sum(e.budget_cents for e in committed) / 100, 2)
    perf_eur = round(max(0.0, perf_eur - committed_eur), 2)
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
        reason = ("dienas budžetu jau tur aktīvās reklāmas — rindā"
                  if committed_eur else "dienas budžets par mazu "
                  f"(min {MIN_AD_BUDGET_EUR:.0f} € reklāmai)")
        for e in picked:
            rejected.append({**e, "reason": reason})
    return {"settings": cfg, "planned": rows, "rejected": rejected,
            "brand_eur": brand_eur, "perf_eur": perf_eur,
            "committed_eur": committed_eur}


def sync_entries(session, now=None) -> int:
    """Materialise the current plan as AdEntry rows (dry/approve modes).
    Planned rows are refreshed in place; entries whose post fell out of the
    plan go back to 'rejected' with the newest reason."""
    now = now or utcnow()
    plan = build_plan(session, now)
    by_post = {e.post_id: e for e in session.execute(
        select(AdEntry).where(AdEntry.status.in_(("candidate", "planned")))
    ).scalars().all()}
    busy = {e.post_id for e in session.execute(
        select(AdEntry).where(AdEntry.status.in_(
            ("awaiting_approval", "active", "paused", "done")))
    ).scalars().all()}
    changed = 0
    for row in plan["planned"]:
        post = row["post"]
        if post.id in busy:
            continue  # jau palaists vai gaida apstiprinājumu — neaiztiekam
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


# --- creative variants ----------------------------------------------------

def ad_copy_variants(article: Article, session) -> list[str]:
    """2-3 ad text variants with different hooks. Claude fast model when the
    key is present; the headline is always the safe fallback."""
    from app import claude, config, credentials

    fallback = [article.title]
    api_key = credentials.get("anthropic_api_key", session)
    if not api_key:
        return fallback
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=config.AI_MODEL_FAST,
            max_tokens=claude.max_tokens_for(config.AI_MODEL_FAST, 300),
            **claude.params(config.AI_MODEL_FAST, "low"),
            messages=[{"role": "user", "content":
                f"Uzraksti 3 īsus (līdz 120 zīmēm) Facebook reklāmas tekstus "
                f"latviski ar DAŽĀDIEM āķiem (fakts, jautājums, skaitlis) "
                f"rakstam. Nevainojama pareizrakstība, bez klikbeita, bez "
                f"pēdiņām. Katru jaunā rindā, bez numerācijas.\n"
                f"Virsraksts: {article.title}\nIevads: {(article.lead or '')[:300]}"}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        variants = [ln.strip() for ln in text.splitlines() if ln.strip()][:3]
        return variants or fallback
    except Exception as e:  # noqa: BLE001
        log.warning("ad variants failed for article %s: %s", article.id, e)
        return fallback


def creative_images(article: Article, post: Post) -> list[str]:
    """Images for the flexible ad: planner-uploaded asset first, then the
    post's own rendered media, then the article photo."""
    out = []
    for asset in (article.creative_assets or []):
        out.append(asset.path)
    for m in (post.media or []):
        if m and not str(m).endswith(".mp4"):
            out.append(str(m))
    for img in (article.images or [])[:1]:
        out.append(img)
    return out[:3]


# --- execution (Phase 1/2: approve + auto) --------------------------------

CAMPAIGNS = {"traffic": ("TV3 Autopilots · Konversijas", "OUTCOME_TRAFFIC"),
             "awareness": ("TV3 Autopilots · Zīmols", "OUTCOME_AWARENESS")}


def ensure_campaign(session, client, kind: str) -> str:
    key = f"ads:campaign_{kind}"
    existing = get_setting(session, key, "")
    if existing:
        return existing
    name, objective = CAMPAIGNS[kind]
    campaign_id = client.create_campaign(name, objective)
    client.set_status(campaign_id, "ACTIVE")
    set_setting(session, key, campaign_id)
    return campaign_id


def launch_entry(session, client, entry: AdEntry) -> None:
    """One planned entry -> one ACTIVE ad set with two ads inside: the boost
    of the organic post (social proof) and a flexible multi-asset dark ad
    (Meta optimizes between them, so creative A/B costs no extra budget)."""
    from app.best_practices import add_utm

    post, article = entry.post, entry.article
    campaign = ensure_campaign(session, client,
                               "traffic" if entry.objective == "traffic"
                               else "awareness")
    name = f"a{entry.id} {article.title[:40]}"
    adset_id = client.create_adset(campaign, name, entry.budget_cents)
    ads_made = []
    if post.platform_post_id:
        ads_made.append(client.create_ad_from_post(
            adset_id, f"{name} · boost", post.platform_post_id))
    # dark flexible ad: clean paid UTM -> GA4 sessions per THIS ad entry
    link = add_utm(post.link_url, "facebook_paid", f"a{entry.id}",
                   hook=post.hook_type or "")
    bodies = ad_copy_variants(article, session)
    hashes = []
    for img in creative_images(article, post):
        try:
            h = client.upload_image(img)
            if h:
                hashes.append(h)
        except Exception as e:  # noqa: BLE001 — attēls nav obligāts
            log.warning("ad image upload failed (%s): %s", img[:60], e)
    try:
        entry.dark_ad_id = client.create_flexible_ad(
            adset_id, f"{name} · varianti", link,
            bodies, [article.title], hashes)
        ads_made.append(entry.dark_ad_id)
    except Exception as e:  # noqa: BLE001 — boosts vien arī ir vērtīgs
        log.warning("flexible ad failed for entry %s: %s", entry.id, e)
    if not ads_made:
        raise RuntimeError("nevienu reklāmu neizdevās izveidot")
    for ad_id in ads_made:
        client.set_status(ad_id, "ACTIVE")
    client.set_status(adset_id, "ACTIVE")
    entry.campaign_id = campaign
    entry.adset_id = adset_id
    entry.ad_id = ads_made[0]
    entry.status = "active"
    entry.updated_at = utcnow()
    session.commit()


def execute(session, client) -> int:
    """Planned entries -> awaiting_approval (approve mode) or straight to
    live (auto mode). Returns how many went live."""
    cfg = settings(session)
    launched = 0
    rows = session.execute(
        select(AdEntry).where(AdEntry.status.in_(("planned", "awaiting_approval")))
    ).scalars().all()
    for entry in rows:
        if cfg["mode"] == "approve" and entry.status == "planned":
            entry.status = "awaiting_approval"
            entry.updated_at = utcnow()
            continue
        if cfg["mode"] == "auto" and entry.status in ("planned",):
            try:
                launch_entry(session, client, entry)
                launched += 1
            except Exception as e:  # noqa: BLE001
                entry.status = "rejected"
                entry.reason = f"palaišana neizdevās: {e}"
                entry.updated_at = utcnow()
    session.commit()
    return launched


# --- measurement + reallocation ------------------------------------------

def collect_metrics(session, client) -> None:
    """Meta insights (spend/clicks) + GA4 paid sessions onto active entries."""
    active = session.execute(
        select(AdEntry).where(AdEntry.status.in_(("active", "paused")))
    ).scalars().all()
    if not active:
        return
    try:
        rows = client.insights(level="ad")
    except Exception as e:  # noqa: BLE001
        log.warning("ads insights failed: %s", e)
        rows = []
    by_ad = {str(r.get("ad_id")): r for r in rows}
    from app import ga4

    paid = ga4.paid_sessions(session)
    for entry in active:
        spend = clicks = imps = 0
        for ad_id in (entry.ad_id, entry.dark_ad_id):
            r = by_ad.get(str(ad_id)) if ad_id else None
            if r:
                spend += int(round(float(r.get("spend") or 0) * 100))
                clicks += int(r.get("inline_link_clicks") or r.get("clicks") or 0)
                imps += int(r.get("impressions") or 0)
        entry.spent_cents = spend
        entry.clicks = clicks
        entry.impressions = imps
        entry.sessions = int(paid.get(f"a{entry.id}", 0) or 0)
        entry.updated_at = utcnow()
    session.commit()


def _score(entry: AdEntry) -> float:
    """Rezultāti par eiro: GA4 sesijas, kad tās jau ienāk, citādi klikšķi."""
    spent = max(1, entry.spent_cents)
    result = entry.sessions if entry.sessions else entry.clicks
    return result * 100.0 / spent


MIN_SPEND_BEFORE_JUDGING = 500       # 5 € — pirms tam nevienu nesodām
PAUSE_BELOW_FACTOR = 0.35            # zem 35% no mediānas -> pauze
REALLOC_STEP = 0.2                   # learning phase: budžets ±20% dienā


def reallocate(session, client, now=None) -> None:
    """Once per day: winners get more (capped at +20%), clear losers pause.
    Runs on measured results only — entries below the spend floor are left
    to learn."""
    now = now or utcnow()
    if get_setting(session, "ads:last_realloc", "") == now.strftime("%Y-%m-%d"):
        return
    active = [e for e in session.execute(
        select(AdEntry).where(AdEntry.status == "active")).scalars().all()
        if e.spent_cents >= MIN_SPEND_BEFORE_JUDGING]
    if len(active) < 2:
        return  # vēl nav ar ko salīdzināt — dienas slotu netērējam
    if active:
        scores = [_score(e) for e in active]
        benchmark = sum(scores) / len(scores)
        for entry in active:
            score = _score(entry)
            if benchmark > 0 and score < benchmark * PAUSE_BELOW_FACTOR:
                try:
                    client.set_status(entry.adset_id, "PAUSED")
                    entry.status = "paused"
                    entry.reason = (f"auto-pauze: {score:.1f} rezultāti/€ pret "
                                    f"vidējo {benchmark:.1f}")
                except Exception as e:  # noqa: BLE001
                    log.warning("pause failed for entry %s: %s", entry.id, e)
                continue
            factor = 1 + REALLOC_STEP if score >= benchmark else 1 - REALLOC_STEP
            new_budget = max(int(MIN_AD_BUDGET_EUR * 100),
                             int(entry.budget_cents * factor))
            if new_budget != entry.budget_cents:
                try:
                    client.set_daily_budget(entry.adset_id, new_budget)
                    entry.budget_cents = new_budget
                except Exception as e:  # noqa: BLE001
                    log.warning("budget update failed for entry %s: %s", entry.id, e)
            entry.updated_at = now
        # dienu atzīmējam tikai pēc reālas pārdales — pirmajos tikšos bez
        # iztērēta budžeta slots paliek pieejams vēlākam stundas ciklam
        set_setting(session, "ads:last_realloc", now.strftime("%Y-%m-%d"))
    session.commit()


def tick(session) -> None:
    """Hourly scheduler step. Off = nothing; dry = plan only; approve = plan
    + queue for the human; auto = the full loop (launch, measure, reallocate).
    Ads must never take publishing down, so everything is caught."""
    from adapters.meta_ads import MetaAdsClient

    cfg = settings(session)
    if cfg["mode"] == "off":
        return
    try:
        sync_entries(session)
        if cfg["mode"] == "dry":
            return
        client = MetaAdsClient(session)
        if not client.configured():
            return
        execute(session, client)
        collect_metrics(session, client)
        if cfg["mode"] == "auto":
            reallocate(session, client)
    except Exception as e:  # noqa: BLE001 — ads nekad nedrīkst gāzt publicēšanu
        log.warning("ads tick failed: %s", e)
