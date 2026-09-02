"""Diversity-aware format selection.

Picks the best format for a specific post instead of defaulting to link:
  score = channel weight (channels.yaml format_weights, editor-tunable)
        × suitability for THIS article (does it have images? a gallery?)
        × feed-diversity multiplier (formats overused in the channel's last
          posts are discounted, underused ones boosted)
  and the AI's explicit choice gets a bonus so it wins unless the feed is
  already saturated with that format.

Once Phase 3 metrics exist, format_weights get replaced by measured
sessions-per-post — the mechanism stays the same.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from app.models import Article, Post

log = logging.getLogger(__name__)

# Fallback weights when a channel doesn't configure format_weights.
DEFAULT_FORMAT_WEIGHTS = {
    "link": 1.0, "photo": 0.9, "photo_album": 0.8,
    "text_only": 0.6, "carousel": 0.7, "video": 0.9, "story": 0.8,
}

DIVERSITY_WINDOW = 6
AI_CHOICE_BONUS = 1.25

# Cik reižu pēc kārtas viens formāts drīkst atkārtoties kanālā. Trešais
# vienāds ieraksts pēc kārtas ir tas, ko lasītājs redz kā vienveidību —
# skaits dienā to nenoķer (6 karuseļi ar diviem foto starpā ir kvotā, bet
# plūsma tik un tā ir vienāda). Kanālā: `max_same_format_in_row`.
DEFAULT_MAX_SAME_IN_ROW = 2

# Griesti formāta daļai pēdējos DIVERSITY_WINDOW ierakstos. Atšķirībā no
# `format_mix` (grīda) šie ir maksimumi: tie neļauj vienam formātam paņemt
# plūsmu arī tad, kad dienas kvota vēl ir brīva. Kanālā: `format_max_share`.
DEFAULT_FORMAT_MAX_SHARE = {"card_carousel": 0.35, "reel": 0.35, "photo": 0.5}

# Minimum share of the recent window a format must keep when the channel
# doesn't configure format_mix. Boostot var visus trīs formātus (saites
# kartīte ar CTA, karuseļa kartītes ir saites, foto ierakstam saite ir
# aprakstā un komentārā) — grīdas jēga ir cita: bez tās viens formāts paņem
# plūsmu, un tad tas ir vienīgais, par kuru mums vispār ir mērījumi.
DEFAULT_FORMAT_MIX: dict[str, float] = {}


def suitable_formats(article: Article, allowed: list[str]) -> list[str]:
    images = article.images or []
    out = []
    for fmt in allowed:
        if fmt in ("card_carousel", "reel"):
            # only the AI proposes carousels/reels (both need card_points);
            # the diversity engine never forces one — handled in the pipeline
            continue
        if fmt in ("photo", "story") and not images:
            continue
        if fmt == "photo_album" and len(images) < 4:
            continue
        if fmt == "carousel" and len(images) < 2:
            continue
        if fmt == "video":  # native video is phase 2+
            continue
        if fmt == "link" and not (article.canonical_url or article.url):
            continue
        out.append(fmt)
    if out:
        return out
    # nothing suitable: stay within the channel's declared formats (the
    # pipeline blocks with a reason if the pick can't be produced) rather
    # than inventing a format the channel never asked for
    if allowed:
        return list(allowed[:1])
    return ["link"] if article.url else ["text_only"]


def recent_formats(session, channel: str, limit: int = DIVERSITY_WINDOW) -> list[str]:
    """Kanāla pēdējie formāti, jaunākais pirmais (ieplānotie skaitās līdzi —
    tie plūsmā jau ir, tikai vēl nav izgājuši)."""
    return list(session.execute(
        select(Post.format).where(Post.channel == channel,
                                  Post.state.in_(("scheduled", "publishing", "published")))
        .order_by(Post.created_at.desc()).limit(limit)
    ).scalars().all())


def recent_format_shares(session, channel: str) -> dict[str, float]:
    rows = recent_formats(session, channel)
    if not rows:
        return {}
    return {f: rows.count(f) / len(rows) for f in set(rows)}


def format_run(session, channel: str) -> tuple[str, int]:
    """(formāts, cik reižu pēc kārtas) kanāla plūsmas galā."""
    rows = recent_formats(session, channel)
    if not rows:
        return "", 0
    head = rows[0]
    run = 0
    for fmt in rows:
        if fmt != head:
            break
        run += 1
    return head, run


def row_limit(cfg: dict) -> int:
    """Cik reižu pēc kārtas viens formāts drīkst atkārtoties (0 = sargs
    izslēgts). `or` te nedrīkst: tas apzināto nulli klusi pārvērstu par
    noklusējumu."""
    value = cfg.get("max_same_format_in_row")
    return DEFAULT_MAX_SAME_IN_ROW if value is None else int(value)


def repeats_too_much(session, channel: str, cfg: dict, fmt: str) -> bool:
    """Vai šis formāts kanālā jau ir bijis pēc kārtas tik reižu, ka nākamais
    tāds pats būtu vienveidība."""
    limit = row_limit(cfg)
    if limit <= 0:
        return False
    head, run = format_run(session, channel)
    return head == fmt and run >= limit


def over_max_share(session, channel: str, cfg: dict, fmt: str,
                   shares: dict[str, float] | None = None) -> bool:
    """Vai formāts jau aizņem lielāku daļu pēdējo ierakstu, nekā tam atļauts."""
    ceilings = {**DEFAULT_FORMAT_MAX_SHARE, **(cfg.get("format_max_share") or {})}
    ceiling = ceilings.get(fmt)
    if ceiling is None:
        return False
    shares = recent_format_shares(session, channel) if shares is None else shares
    return bool(shares) and shares.get(fmt, 0.0) >= float(ceiling)


def monotony_state(session, channel: str, cfg: dict, fmt: str) -> tuple[int, str]:
    """(cik slikti, iemesls) — 0 tīrs, 1 virs daļas griestiem, 2 atkārtojas
    pēc kārtas. Pakāpes vajag tāpēc, ka reizēm VISI formāti ir pāri robežai:
    tad jāizvēlas mazākais ļaunums, ne jāatlaiž sargs pavisam (tieši tur
    plūsma vienreiz sabruka atpakaļ vienā formātā)."""
    if repeats_too_much(session, channel, cfg, fmt):
        _, run = format_run(session, channel)
        return 2, (f"pēdējie {run} ieraksti jau ir {fmt} "
                   f"(limits {row_limit(cfg)} pēc kārtas)")
    if over_max_share(session, channel, cfg, fmt):
        share = recent_format_shares(session, channel).get(fmt, 0.0)
        ceilings = {**DEFAULT_FORMAT_MAX_SHARE, **(cfg.get("format_max_share") or {})}
        return 1, (f"{fmt} jau aizņem {share:.0%} pēdējo ierakstu "
                   f"(griesti {float(ceilings[fmt]):.0%})")
    return 0, ""


def monotony_reason(session, channel: str, cfg: dict, fmt: str) -> str:
    """Kāpēc šis formāts tagad nedrīkst atkārtoties ('' = drīkst)."""
    return monotony_state(session, channel, cfg, fmt)[1]


MIN_ADS_PER_FORMAT = 3        # zem šī viena veiksmīga reklāma izšķirtu visu
MIN_AD_SPEND_CENTS = 300      # 3 € — zem tā skaitlis ir troksnis
AD_BIAS_RANGE = (0.85, 1.2)   # reklāmas arguments koriģē, ne izšķir


def ad_multipliers(session, channel: str) -> dict[str, float]:
    """{formāts: reizinātājs} pēc IZMĒRĪTIEM maksas rezultātiem.

    Boostot var visus trīs formātus, un tie nav vienlīdzīgi: saites
    ierakstam mērķa saite ir kartītē ar CTA pogu, karuseļa katra kartīte ir
    sava saite, foto ierakstam saite ir aprakstā un pirmajā komentārā.
    Kurš no tiem par eiro atved vairāk sesiju, nav jāmin — reklāmu ieraksti
    (AdEntry) to mēra. Kamēr datu nav, reizinātājs ir 1.0 un formāta izvēli
    lemj tikai organiskie signāli.

    Piemēro tikai tad, kad reklāmas tiešām iet ārā: dry/off režīmā maksas
    arguments neko nenozīmē.
    """
    from app import config
    from app.models import AdEntry, get_setting

    if not config.load_rules().get("ads_inform_format", True):
        return {}
    if get_setting(session, "ads:mode", "off") not in ("approve", "auto"):
        return {}
    rows = session.execute(
        select(Post.format, AdEntry.spent_cents, AdEntry.sessions, AdEntry.clicks)
        .join(Post, Post.id == AdEntry.post_id)
        .where(Post.channel == channel,
               AdEntry.status.in_(("active", "paused", "done")),
               AdEntry.spent_cents >= MIN_AD_SPEND_CENTS)
    ).all()
    by_format: dict[str, list[float]] = {}
    for fmt, spent, sessions, clicks in rows:
        result = sessions or clicks or 0
        by_format.setdefault(fmt, []).append(result * 100.0 / max(1, spent))
    scored = {f: sum(v) / len(v) for f, v in by_format.items()
              if len(v) >= MIN_ADS_PER_FORMAT}
    if len(scored) < 2:
        return {}    # salīdzināt nav ar ko
    mean = sum(scored.values()) / len(scored)
    if mean <= 0:
        return {}
    low, high = AD_BIAS_RANGE
    return {f: max(low, min(high, v / mean)) for f, v in scored.items()}


def mix_deficit(shares: dict[str, float], targets: dict, candidates: list[str]) -> str | None:
    """The candidate furthest below its configured minimum share, if any.

    A floor beats scoring: without it a format that starts slightly ahead
    wins every tie, takes the whole feed, and is then the only format with
    measured data — so it keeps winning.
    """
    worst, gap = None, 0.0
    for fmt in candidates:
        target = float(targets.get(fmt) or 0)
        deficit = target - shares.get(fmt, 0.0)
        if target > 0 and deficit > gap:
            worst, gap = fmt, deficit
    return worst


def explain(session, channel: str, channel_cfg: dict, article: Article,
            ai_choice: str | None = None) -> dict:
    """Formāta izvēle ar VISU pamatojumu — viens avots gan lēmumam, gan
    žurnālam un diagnostikas atskaitei (`scripts/format_report.py`).

    Atgriež: chosen, allowed, candidates, blocked {formāts: iemesls},
    shares, run, decision (kāpēc tieši šis), scores {formāts: skaitlis}.
    """
    allowed = list(channel_cfg.get("formats") or ["link"])
    candidates = suitable_formats(article, allowed)
    head, run = format_run(session, channel)
    shares = recent_format_shares(session, channel)
    trace = {"channel": channel, "allowed": allowed, "suitable": list(candidates),
             "shares": {f: round(v, 3) for f, v in shares.items()},
             "run": {"format": head, "count": run},
             "ai_choice": ai_choice or "", "blocked": {}, "scores": {}}
    unsuitable = [f for f in allowed if f not in candidates]
    for fmt in unsuitable:
        trace["blocked"][fmt] = "formāts šim rakstam neder (attēli/saite/AI izvēle)"
    if len(candidates) == 1:
        trace.update(chosen=candidates[0], decision="vienīgais derīgais formāts")
        return trace

    weights = {**DEFAULT_FORMAT_WEIGHTS, **(channel_cfg.get("format_weights") or {})}
    # Vienveidības sargs: formātu, kas jau atkārtojas pēc kārtas vai aizņem
    # vairāk par saviem griestiem, šoreiz vispār neizskatām — tieši tas
    # padara plūsmu par vienu un to pašu. Ja pāri nepaliek nekas, sargu
    # atlaižam: labāk atkārtojums nekā neizsūtīts saturs.
    monotony = {f: monotony_state(session, channel, channel_cfg, f) for f in candidates}
    least = min(p for p, _ in monotony.values())
    for fmt, (penalty, why) in monotony.items():
        if penalty > least:
            trace["blocked"][fmt] = why
    candidates = [f for f in candidates if monotony[f][0] == least]
    if least > 0:
        # neviens formāts nav tīrs: ejam ar mazāko ļaunumu, nevis atlaižam
        # sargu pavisam — «pēc kārtas» ir smagāks pārkāpums nekā daļas griesti
        trace["note"] = ("neviens formāts nav tīrs; izvēlamies mazāko ļaunumu: "
                         + ", ".join(f"{f} ({monotony[f][1]})" for f in candidates))
    trace["candidates"] = list(candidates)

    floors = {**DEFAULT_FORMAT_MIX, **(channel_cfg.get("format_mix") or {})}
    starved = mix_deficit(shares, floors, candidates)
    if starved:
        trace.update(chosen=starved,
                     decision=(f"grīda: {starved} daļa {shares.get(starved, 0.0):.0%} "
                               f"zem {float(floors[starved]):.0%}"))
        return trace

    # measured sessions-per-post adjusts the configured weights (priors.py)
    from app import priors

    measured = priors.format_multipliers(session, channel)

    # Visual stories lean photo; hard news leans link (best CTR to the site).
    section_bias = {}
    if article.section == "entertainment" and (article.images or []):
        section_bias["photo"] = 1.15
        section_bias["photo_album"] = 1.15
    elif article.section in ("news", "sport"):
        section_bias["link"] = 1.1

    # reklāmas arguments: kurš formāts par eiro tiešām atved vairāk sesiju
    paid = ad_multipliers(session, channel)

    best, best_score = candidates[0], -1.0
    for fmt in candidates:
        parts = {
            "svars": float(weights.get(fmt, 0.5)),
            "izmērītais": measured.get(fmt, 1.0),
            "sadaļa": section_bias.get(fmt, 1.0),
            "reklāma": paid.get(fmt, 1.0),
            "piesātinājums": 1.3 - shares.get(fmt, 0.0),
            "AI izvēle": AI_CHOICE_BONUS if fmt == ai_choice else 1.0,
        }
        score = 1.0
        for value in parts.values():
            score *= value
        trace["scores"][fmt] = {"total": round(score, 3),
                                **{k: round(v, 3) for k, v in parts.items()}}
        if score > best_score:
            best, best_score = fmt, score
    trace.update(chosen=best, decision=f"augstākais rezultāts {best_score:.2f}")
    return trace


def choose_format(session, channel: str, channel_cfg: dict, article: Article,
                  ai_choice: str | None = None) -> str:
    trace = explain(session, channel, channel_cfg, article, ai_choice)
    log.info("format %s: %s (%s; daļas %s; pēc kārtas %s×%s; bloķēti %s)",
             channel, trace["chosen"], trace.get("decision", ""),
             trace.get("shares"), trace["run"]["format"], trace["run"]["count"],
             trace.get("blocked") or "-")
    return trace["chosen"]
