"""Ads autopilot orchestration — Meta un Google vienā budžeta ciklā.

Every cycle: pick boostable candidates from recently published Facebook
posts, allocate the configured daily budget across them and across the
connected ad platforms, and record the plan as AdEntry rows. In dry mode
nothing leaves the building — the /ads page shows exactly what WOULD be
boosted and for how much. Live modes turn the same plan into campaigns via
adapters.meta_ads (boosts + variants) and adapters.google_ads (Demand Gen
for Discover traffic, Display for brand reach, Search for brand queries).

Budžeta struktūra (docs/ads-strategy.md):
  dienas budžets = zīmola daļa (brand_share %) + konversiju daļa
  konversiju daļa dalās starp Meta un Google (google_share %) un tālāk
    starp rakstiem — katrs saņem vismaz MIN_AD_BUDGET_EUR;
  zīmola daļa: vispirms vienmēr ieslēgtā Google zīmola meklēšana
    (brand_search_daily €), atlikums — zīmola franšīzēm (Dienas TOP 3,
    Nedēļas TOP 5, Nedēļa 30 sekundēs) kā sasniedzamības kampaņas.

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


# --- platforms ------------------------------------------------------------

PLATFORM_LABELS = {"facebook_page": "Meta", "google_ads": "Google"}
OBJECTIVE_LABELS = {"traffic": "klikšķi", "awareness": "zīmols",
                    "brand_search": "zīmola meklēšana"}


def client_for(platform: str, session=None):
    """Reklāmu klients platformai (nekonfigurēts klients ir OK — pārbaudi
    `configured()`)."""
    if platform == "google_ads":
        from adapters.google_ads import GoogleAdsClient

        return GoogleAdsClient(session)
    from adapters.meta_ads import MetaAdsClient

    return MetaAdsClient(session)


def clients(session=None) -> dict:
    """{platforma: klients} tikai pieslēgtajiem kontiem."""
    out = {}
    for platform in PLATFORM_LABELS:
        c = client_for(platform, session)
        if c.configured():
            out[platform] = c
    return out


def plan_platforms(session=None) -> list[str]:
    """Kurām platformām plānot. Pieslēgtās; bez neviena konta — Meta (dry
    priekšskatījums tāds pats kā līdz šim)."""
    return list(clients(session)) or ["facebook_page"]


def _clients_map(client) -> dict:
    if client is None:
        return {}
    if isinstance(client, dict):
        return client
    return {getattr(client, "platform", "facebook_page"): client}


# --- settings -------------------------------------------------------------

DEFAULT_BRAND_KEYWORDS = "tv3, tv3 ziņas, tv3 zinas, tv3.lv, tv3 lv, tv3 play"


def settings(session) -> dict:
    mode = get_setting(session, "ads:mode", "off")
    return {
        "mode": mode if mode in MODES else "off",
        "daily_budget": float(get_setting(session, "ads:daily_budget", "0") or 0),
        "brand_share": int(get_setting(session, "ads:brand_share", "20") or 20),
        # cik % no konversiju budžeta iet Google (Demand Gen), pārējais Meta
        "google_share": int(get_setting(session, "ads:google_share", "50") or 0),
        # TV3 Play promo daļa konversiju budžetā (docs/play-strategy.md: ≤ 15 %)
        "play_share": int(get_setting(session, "ads:play_share", "15") or 0),
        # vienmēr ieslēgtā zīmola meklēšana; 0 = izslēgta
        "brand_search_daily": float(get_setting(session, "ads:brand_search_daily", "3") or 0),
        "brand_keywords": get_setting(session, "ads:brand_keywords", DEFAULT_BRAND_KEYWORDS),
    }


def save_settings(session, mode: str, daily_budget: float, brand_share: int,
                  google_share: int | None = None,
                  brand_search_daily: float | None = None,
                  brand_keywords: str | None = None) -> None:
    set_setting(session, "ads:mode", mode if mode in MODES else "off")
    set_setting(session, "ads:daily_budget", str(max(0.0, daily_budget)))
    set_setting(session, "ads:brand_share", str(min(50, max(0, brand_share))))
    if google_share is not None:
        set_setting(session, "ads:google_share", str(min(100, max(0, int(google_share)))))
    if brand_search_daily is not None:
        set_setting(session, "ads:brand_search_daily", str(max(0.0, float(brand_search_daily))))
    if brand_keywords is not None:
        set_setting(session, "ads:brand_keywords", brand_keywords.strip() or DEFAULT_BRAND_KEYWORDS)


def brand_keywords(cfg: dict) -> list[str]:
    return [k.strip() for k in str(cfg.get("brand_keywords") or "").split(",") if k.strip()]


# --- boostability ---------------------------------------------------------

def politics_hit(article: Article) -> str:
    text = f"{article.title} {article.lead or ''}".lower()
    for stem in POLITICS_STEMS:
        if stem in text:
            return stem
    return ""


def boostable(article: Article) -> tuple[bool, str]:
    """(drīkst boostot, iemesls). Veto slāņi pirms AI viedokļa."""
    if (article.raw_json or {}).get("_play"):
        return True, "TV3 Play promo — trafiks uz play.tv3.lv"
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
# zīmola franšīzes: ierakstu hook_type marķieri (app.weekend), ko rādām kā
# TV3.lv zīmolu, ne kā vienu rakstu — sasniedzamības kampaņu kreatīvs
FRANCHISE_MARKERS = ("dailystory", "mondaytop5", "digest", "digestreel", "number",
                     "mondaystory", "guide", "icymi", "quiz", "question", "yearago",
                     "evergreen")
AWARENESS_FORMATS = ("card_carousel", "photo", "story")
AWARENESS_WINDOW_HOURS = 7 * 24
MAX_AWARENESS_ADS = 1          # uz platformu vienlaikus: zīmols ir pastāvīgs, ne plūdi
BRAND_SEARCH_ARTICLE_GUID = "ads-brand-search"


def _busy(session) -> set[tuple[int, str]]:
    return {(e.post_id, e.platform) for e in session.execute(
        select(AdEntry).where(AdEntry.status.in_(
            ("awaiting_approval", "active", "paused", "done")))
    ).scalars().all()}


def candidates(session, now=None, platform: str = "facebook_page") -> tuple[list[dict], list[dict]]:
    """(izvēlētie, noraidītie ar iemesliem) no pēdējo 48 h FB ierakstiem.

    Kandidāti ir tie paši visām platformām (raksts, ko drīkst reklamēt,
    drīkst to gan Meta, gan Google), bet aizņemtība skaitās pa platformām."""
    now = now or utcnow()
    since = now - timedelta(hours=CANDIDATE_WINDOW_HOURS)
    rows = session.execute(
        select(Post).where(Post.state == "published",
                           Post.published_at >= since,
                           Post.channel == "fb_tv3lv",
                           Post.format.in_(BOOSTABLE_FORMATS))
        .order_by(Post.published_at.desc())
    ).scalars().all()
    busy = _busy(session)
    picked: list[dict] = []
    rejected: list[dict] = []
    seen_articles: set[int] = set()
    for post in rows:
        if (post.id, platform) in busy:
            continue  # jau reklāmā — budžetu tam tur aktīvais ieraksts
        art = post.article
        if art is None or not post.link_url:
            continue
        is_play = bool((art.raw_json or {}).get("_play"))
        if art.id in seen_articles or ((art.raw_json or {}).get("_digest") and not is_play):
            continue
        seen_articles.add(art.id)
        ok, reason = boostable(art)
        score = float(art.ai_score or 0)
        if ok and is_play:
            # Play boostojam tikai to, kas organiski jau strādā (P3)
            ok, reason, score = _play_threshold(session, post)
        entry = {"post": post, "article": art, "reason": reason,
                 "score": score, "platform": platform, "play": is_play}
        (picked if ok else rejected).append(entry)
    picked.sort(key=lambda e: -e["score"])
    return picked[:MAX_ACTIVE_ADS], rejected


def _play_threshold(session, post: Post) -> tuple[bool, str, float]:
    """Play ieraksts drīkst reklāmā, ja organiski sasniedzis slieksni."""
    from sqlalchemy import func

    from app import play
    from app.models import PostMetrics

    cfg = play.settings()
    imp, clicks = session.execute(
        select(func.max(PostMetrics.impressions), func.max(PostMetrics.clicks))
        .where(PostMetrics.post_id == post.id)).one()
    imp, clicks = int(imp or 0), int(clicks or 0)
    if play.somber(session)[0]:
        return False, "drūma diena — Play reklāmas pauzētas", 0.0
    if imp < int(cfg.get("boost_min_impressions") or 0) and clicks < int(cfg.get("boost_min_clicks") or 0):
        return False, (f"Play: organiski vēl {imp} sasniegti / {clicks} klikšķi — "
                       f"slieksnis {cfg.get('boost_min_impressions')} / {cfg.get('boost_min_clicks')}"), 0.0
    score = play.title_scores(session).get(str(play.play_data(post.article).get("show_id")), 0.0)
    return True, f"Play organiski strādā ({imp} sasniegti, {clicks} klikšķi)", score


def franchise_candidates(session, now=None, platform: str = "facebook_page") -> list[dict]:
    """Jaunākie zīmola franšīžu ieraksti sasniedzamības kampaņai (0–1)."""
    now = now or utcnow()
    since = now - timedelta(hours=AWARENESS_WINDOW_HOURS)
    rows = session.execute(
        select(Post).where(Post.state == "published", Post.published_at >= since,
                           Post.hook_type.in_(FRANCHISE_MARKERS),
                           Post.format.in_(AWARENESS_FORMATS))
        .order_by(Post.published_at.desc())
    ).scalars().all()
    busy = _busy(session)
    out = []
    for post in rows:
        if (post.id, platform) in busy or post.article is None:
            continue
        media = [m for m in (post.media or []) if m and not str(m).endswith(".mp4")]
        if not media and not (post.article.images or []):
            continue
        out.append({"post": post, "article": post.article, "platform": platform,
                    "score": 0.0, "reason": "zīmola franšīze — sasniedzamība, ne klikšķi"})
        if len(out) >= MAX_AWARENESS_ADS:
            break
    return out


def brand_post(session) -> Post:
    """Sintētisks ieraksts, pie kā turēt vienmēr ieslēgto zīmola meklēšanas
    kampaņu (AdEntry vienmēr pieder ierakstam). Stāvoklis «internal» —
    rindā un vēsturē tas neparādās."""
    art = session.execute(
        select(Article).where(Article.guid == BRAND_SEARCH_ARTICLE_GUID)).scalar_one_or_none()
    if art is None:
        art = Article(guid=BRAND_SEARCH_ARTICLE_GUID, url="https://tv3.lv/",
                      canonical_url="https://tv3.lv/", title="TV3.lv — zīmola meklēšana",
                      section="news", editor_status="can",
                      raw_json={"_digest": True, "_brand": True})
        session.add(art)
        session.flush()
    post = session.execute(
        select(Post).where(Post.article_id == art.id, Post.format == "brand_search")
    ).scalars().first()
    if post is None:
        post = Post(article_id=art.id, channel="google_ads", format="brand_search",
                    copy="TV3.lv", link_url="https://tv3.lv/", state="internal",
                    hook_type="brand")
        session.add(post)
        session.flush()
    return post


def _split(total: float, count: int) -> list[float]:
    """Vienmērīgi, ar vismaz MIN_AD_BUDGET_EUR katram; cik ietilpst."""
    if total < MIN_AD_BUDGET_EUR or count <= 0:
        return []
    n = min(count, max(1, int(total // MIN_AD_BUDGET_EUR)))
    return [round(total / n, 2)] * n


def build_plan(session, now=None) -> dict:
    """Dienas plāns: kandidāti + budžeta sadale pa platformām un mērķiem.
    Tikai aprēķins, bez API."""
    now = now or utcnow()
    cfg = settings(session)
    platforms = plan_platforms(session)
    budget = cfg["daily_budget"]
    brand_eur = round(budget * cfg["brand_share"] / 100, 2)
    perf_eur = round(budget - brand_eur, 2)
    # jau aktīvās reklāmas tērē savu dienas budžetu — jaunie kandidāti dala
    # tikai atlikumu, citādi kopējais tēriņš pārsniegtu dienas limitu
    committed = session.execute(
        select(AdEntry).where(AdEntry.status.in_(("awaiting_approval", "active")))
    ).scalars().all()
    committed_perf = round(sum(e.budget_cents for e in committed
                               if e.objective == "traffic") / 100, 2)
    committed_brand = round(sum(e.budget_cents for e in committed
                                if e.objective != "traffic") / 100, 2)
    perf_avail = round(max(0.0, perf_eur - committed_perf), 2)
    brand_avail = round(max(0.0, brand_eur - committed_brand), 2)

    # konversiju budžets pa platformām
    if len(platforms) == 1:
        perf_by_platform = {platforms[0]: perf_avail}
    else:
        g = round(perf_avail * cfg["google_share"] / 100, 2)
        perf_by_platform = {"google_ads": g, "facebook_page": round(perf_avail - g, 2)}
    rows: list[dict] = []
    rejected: list[dict] = []
    seen_rejects: set[int] = set()
    for platform in platforms:
        picked, rej = candidates(session, now, platform)
        for r in rej:
            if r["article"].id not in seen_rejects:
                seen_rejects.add(r["article"].id)
                rejected.append(r)
        avail = perf_by_platform.get(platform, 0.0)
        # Play promo saņem ne vairāk kā play_share no konversiju budžeta
        play_picked = [e for e in picked if e.get("play")]
        news_picked = [e for e in picked if not e.get("play")]
        play_eur = round(avail * cfg.get("play_share", 0) / 100, 2) if play_picked else 0.0
        play_shares = _split(play_eur, len(play_picked))
        for e, share in zip(play_picked, play_shares):
            rows.append({**e, "budget_eur": share, "objective": "traffic"})
        for e in play_picked[len(play_shares):]:
            rejected.append({**e, "reason": f"{PLATFORM_LABELS[platform]}: Play daļa ({cfg.get('play_share')} %) šodien pilna"})
        shares = _split(round(avail - sum(play_shares), 2), len(news_picked))
        picked = news_picked
        for e, share in zip(picked, shares):
            rows.append({**e, "budget_eur": share, "objective": "traffic"})
        if picked and not shares:
            reason = ("dienas budžetu jau tur aktīvās reklāmas — rindā"
                      if committed_perf else "dienas budžets par mazu "
                      f"(min {MIN_AD_BUDGET_EUR:.0f} € reklāmai)")
            for e in picked:
                rejected.append({**e, "reason": f"{PLATFORM_LABELS[platform]}: {reason}"})
        for e in picked[len(shares):]:
            rejected.append({**e, "reason": f"{PLATFORM_LABELS[platform]}: budžets šodien pilns — rindā"})

    # zīmola budžets: vispirms vienmēr ieslēgtā zīmola meklēšana Google
    brand_rows: list[dict] = []
    bs = cfg["brand_search_daily"]
    has_brand_search = any(e.objective == "brand_search" for e in committed)
    if ("google_ads" in platforms and bs > 0 and not has_brand_search
            and brand_avail >= bs):
        post = brand_post(session)
        brand_rows.append({"post": post, "article": post.article, "platform": "google_ads",
                           "objective": "brand_search", "budget_eur": round(bs, 2),
                           "score": 0.0,
                           "reason": "zīmola vaicājumi (tv3, tv3 ziņas, tv3 play) — vienmēr ieslēgts"})
        brand_avail = round(brand_avail - bs, 2)
    # atlikums — franšīžu sasniedzamībai, pa platformām tāpat kā konversijas
    if brand_avail >= MIN_AD_BUDGET_EUR:
        if len(platforms) == 1:
            brand_by_platform = {platforms[0]: brand_avail}
        else:
            g = round(brand_avail * cfg["google_share"] / 100, 2)
            brand_by_platform = {"google_ads": g, "facebook_page": round(brand_avail - g, 2)}
        for platform in platforms:
            picked = franchise_candidates(session, now, platform)
            shares = _split(brand_by_platform.get(platform, 0.0), len(picked))
            for e, share in zip(picked, shares):
                brand_rows.append({**e, "budget_eur": share, "objective": "awareness"})
    return {"settings": cfg, "planned": rows + brand_rows, "rejected": rejected,
            "brand_eur": brand_eur, "perf_eur": perf_avail,
            "committed_eur": round(committed_perf + committed_brand, 2),
            "platforms": platforms}


def sync_entries(session, now=None) -> int:
    """Materialise the current plan as AdEntry rows (dry/approve modes).
    Planned rows are refreshed in place; entries whose post fell out of the
    plan go back to 'rejected' with the newest reason."""
    now = now or utcnow()
    plan = build_plan(session, now)
    by_key = {(e.post_id, e.platform): e for e in session.execute(
        select(AdEntry).where(AdEntry.status.in_(("candidate", "planned")))
    ).scalars().all()}
    busy = _busy(session)
    changed = 0
    for row in plan["planned"]:
        post, platform = row["post"], row["platform"]
        if (post.id, platform) in busy:
            continue  # jau palaists vai gaida apstiprinājumu — neaiztiekam
        entry = by_key.pop((post.id, platform), None)
        if entry is None:
            entry = AdEntry(post_id=post.id, article_id=row["article"].id,
                            platform=platform)
            session.add(entry)
        entry.status = "planned"
        entry.objective = row["objective"]
        entry.budget_cents = int(round(row["budget_eur"] * 100))
        entry.reason = row["reason"]
        entry.updated_at = now
        changed += 1
    for entry in by_key.values():
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


# --- execution (approve + auto) ------------------------------------------

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


def _launch_meta(session, client, entry: AdEntry) -> None:
    """One planned entry -> one ACTIVE ad set with two ads inside: the boost
    of the organic post (social proof) and a flexible multi-asset dark ad
    (Meta optimizes between them, so creative A/B costs no extra budget).
    Zīmola (awareness) ierakstiem tas pats, tikai kampaņa ir sasniedzamības
    un optimizācija — REACH, ne klikšķi."""
    from app.best_practices import add_utm

    post, article = entry.post, entry.article
    awareness = entry.objective != "traffic"
    campaign = ensure_campaign(session, client, "awareness" if awareness else "traffic")
    name = f"a{entry.id} {article.title[:40]}"
    adset_id = client.create_adset(campaign, name, entry.budget_cents,
                                   optimization_goal="REACH" if awareness else "")
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


BRAND_SEARCH_HEADLINES = ["TV3.lv – ziņas un izklaide", "Jaunākās ziņas Latvijā",
                          "TV3 Play video un raidījumi", "Sports, izklaide, notikumi",
                          "Uzticamas ziņas katru dienu"]
BRAND_SEARCH_DESCRIPTIONS = [
    "Svarīgākais Latvijā un pasaulē – ātri, precīzi un ar video. Lasi tv3.lv.",
    "TV3 ziņas, sports, izklaide un TV3 Play raidījumi vienuviet.",
]


def google_texts(entry: AdEntry, session) -> tuple[list[str], list[str]]:
    """(virsraksti, apraksti) Google reklāmai no raksta un AI variantiem."""
    from app.adcreative import fit

    if entry.objective == "brand_search":
        return list(BRAND_SEARCH_HEADLINES), list(BRAND_SEARCH_DESCRIPTIONS)
    article, post = entry.article, entry.post
    variants = ad_copy_variants(article, session)
    limit = 30 if entry.objective == "awareness" else 40
    headlines = [fit(article.title, limit)]
    for v in variants:
        h = fit(v, limit)
        if h and h not in headlines:
            headlines.append(h)
    descriptions = []
    for text in variants + [post.copy or "", article.lead or ""]:
        d = fit(text, 90)
        if d and d not in descriptions:
            descriptions.append(d)
    if not descriptions:
        descriptions = [fit(f"Lasi vairāk tv3.lv — {article.title}", 90)]
    return headlines[:5], descriptions[:5]


def _launch_google(session, client, entry: AdEntry) -> None:
    """One entry -> one Google campaign: Demand Gen (Discover) for traffic,
    Display CPM for brand franchises, Search for brand keywords."""
    from app import adcreative
    from app.best_practices import add_utm

    post, article = entry.post, entry.article
    cfg = settings(session)
    name = f"a{entry.id} {article.title[:40]}"
    link = add_utm(post.link_url or article.canonical_url, "google_paid",
                   f"a{entry.id}", hook=post.hook_type or "")
    headlines, descriptions = google_texts(entry, session)
    images: dict[str, str] = {}
    logo = ""
    keywords = None
    if entry.objective == "brand_search":
        keywords = brand_keywords(cfg) or brand_keywords({"brand_keywords": DEFAULT_BRAND_KEYWORDS})
    else:
        sources = creative_images(article, post)
        if not sources:
            raise RuntimeError("Google reklāmai vajag attēlu, rakstam tāda nav")
        variants = adcreative.image_variants(
            sources[0], need_portrait=entry.objective == "traffic")
        variants["logo"] = adcreative.logo_square()
        uploaded = client.upload_images(variants, name)
        logo = uploaded.pop("logo", "")
        images = uploaded
        if not images:
            raise RuntimeError("attēlus Google kontā augšupielādēt neizdevās")
    made = client.launch(name, entry.objective, link, headlines, descriptions,
                         entry.budget_cents, images=images, logo=logo,
                         keywords=keywords)
    client.set_status(made["campaign_id"], "ACTIVE")
    entry.campaign_id = made["campaign_id"]
    entry.adset_id = made["adset_id"]
    entry.ad_id = made["ad_id"]


def launch_entry(session, client, entry: AdEntry) -> None:
    """Planned entry -> live campaign on its platform."""
    if entry.platform == "google_ads":
        _launch_google(session, client, entry)
    else:
        _launch_meta(session, client, entry)
    entry.status = "active"
    entry.updated_at = utcnow()
    session.commit()


def execute(session, client) -> int:
    """Planned entries -> awaiting_approval (approve mode) or straight to
    live (auto mode). `client` ir viens klients vai {platforma: klients}.
    Returns how many went live."""
    cfg = settings(session)
    by_platform = _clients_map(client)
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
            c = by_platform.get(entry.platform)
            if c is None:
                continue  # šai platformai konta nav — paliek plānā
            try:
                launch_entry(session, c, entry)
                launched += 1
            except Exception as e:  # noqa: BLE001
                entry.status = "rejected"
                entry.reason = f"palaišana neizdevās: {e}"
                entry.updated_at = utcnow()
    session.commit()
    return launched


# --- measurement + reallocation ------------------------------------------

def collect_metrics(session, client) -> None:
    """Platformu insights (spend/clicks) + GA4 paid sessions onto active
    entries, pa platformām."""
    active = session.execute(
        select(AdEntry).where(AdEntry.status.in_(("active", "paused")))
    ).scalars().all()
    if not active:
        return
    by_platform = _clients_map(client)
    rows_by_platform: dict[str, dict] = {}
    for platform, c in by_platform.items():
        try:
            rows = c.insights(level="ad")
        except Exception as e:  # noqa: BLE001
            log.warning("%s ads insights failed: %s", platform, e)
            rows = []
        rows_by_platform[platform] = {str(r.get("ad_id")): r for r in rows}
    from app import ga4

    paid = ga4.paid_sessions(session)
    for entry in active:
        by_ad = rows_by_platform.get(entry.platform)
        if by_ad is None:
            continue  # šai platformai šoreiz datu nav — vecos skaitļus neaiztiekam
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
    to learn. Salīdzina tikai vienas platformas klikšķu reklāmas savā
    starpā: Google un Meta cenas nav salīdzināmas, un zīmola kampaņas
    nemēra ar klikšķiem vispār."""
    now = now or utcnow()
    if get_setting(session, "ads:last_realloc", "") == now.strftime("%Y-%m-%d"):
        return
    by_platform = _clients_map(client)
    touched = False
    for platform, c in by_platform.items():
        active = [e for e in session.execute(
            select(AdEntry).where(AdEntry.status == "active",
                                  AdEntry.platform == platform,
                                  AdEntry.objective == "traffic")).scalars().all()
            if e.spent_cents >= MIN_SPEND_BEFORE_JUDGING]
        if len(active) < 2:
            continue  # vēl nav ar ko salīdzināt — dienas slotu netērējam
        touched = True
        scores = [_score(e) for e in active]
        benchmark = sum(scores) / len(scores)
        for entry in active:
            score = _score(entry)
            if benchmark > 0 and score < benchmark * PAUSE_BELOW_FACTOR:
                try:
                    c.set_status(entry.adset_id, "PAUSED")
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
                    c.set_daily_budget(entry.adset_id, new_budget)
                    entry.budget_cents = new_budget
                except Exception as e:  # noqa: BLE001
                    log.warning("budget update failed for entry %s: %s", entry.id, e)
            entry.updated_at = now
    if touched:
        # dienu atzīmējam tikai pēc reālas pārdales — pirmajos tikšos bez
        # iztērēta budžeta slots paliek pieejams vēlākam stundas ciklam
        set_setting(session, "ads:last_realloc", now.strftime("%Y-%m-%d"))
    session.commit()


def tick(session) -> None:
    """Hourly scheduler step. Off = nothing; dry = plan only; approve = plan
    + queue for the human; auto = the full loop (launch, measure, reallocate)
    on every connected platform. Ads must never take publishing down, so
    everything is caught."""
    cfg = settings(session)
    if cfg["mode"] == "off":
        return
    try:
        sync_entries(session)
        if cfg["mode"] == "dry":
            return
        live = clients(session)
        if not live:
            return
        execute(session, live)
        collect_metrics(session, live)
        if cfg["mode"] == "auto":
            reallocate(session, live)
    except Exception as e:  # noqa: BLE001 — ads nekad nedrīkst gāzt publicēšanu
        log.warning("ads tick failed: %s", e)
