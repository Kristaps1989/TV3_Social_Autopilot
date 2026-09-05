"""Formātu izvēles diagnostika — viens datu avots lapai «Diagnostika»
(`/logs`), JSON eksportam un komandrindas atskaitei
(`scripts/format_report.py`).

Atbild uz vienu jautājumu: kāpēc plūsmā ir tieši šie formāti un kas tieši
tur atpakaļ pārējos.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import desc, select

from app import config, formats, pipeline
from app.models import AdEntry, Article, DecisionLog, Post, get_setting, utcnow

HISTORY_STATES = ("scheduled", "publishing", "published", "cancelled")


def channel_diagnostics(session, name: str, cfg: dict, posts: int = 15) -> dict:
    """Viena kanāla pilnā aina: konfigurācija, logs, sargi, ierakstu iemesli."""
    allowed = list(cfg.get("formats") or [])
    floors = {**formats.DEFAULT_FORMAT_MIX, **(cfg.get("format_mix") or {})}
    ceilings = {**formats.DEFAULT_FORMAT_MAX_SHARE, **(cfg.get("format_max_share") or {})}
    caps = {**pipeline.DEFAULT_FORMAT_DAILY_CAP, **(cfg.get("format_daily_cap") or {})}
    weights = {**formats.DEFAULT_FORMAT_WEIGHTS, **(cfg.get("format_weights") or {})}
    shares = formats.recent_format_shares(session, name)
    head, run = formats.format_run(session, name)
    today = {fmt: pipeline.posts_today(session, name, fmt) for fmt in allowed}

    status = []
    for fmt in allowed:
        penalty, why = formats.monotony_state(session, name, cfg, fmt)
        cap = caps.get(fmt)
        used = today.get(fmt, 0)
        floor = floors.get(fmt)
        blocks = []
        if why:
            blocks.append(why)
        if cap is not None and used >= int(cap):
            blocks.append(f"dienas kvota {used}/{cap} izpildīta")
        status.append({
            "format": fmt,
            "share": round(shares.get(fmt, 0.0), 3),
            "today": used,
            "cap": cap,
            "floor": floor,
            "ceiling": ceilings.get(fmt),
            "weight": weights.get(fmt),
            "penalty": penalty,
            "blocked": "; ".join(blocks),
            "starved": bool(floor and shares.get(fmt, 0.0) < float(floor)),
        })

    rows = session.execute(
        select(Post).where(Post.channel == name, Post.state.in_(HISTORY_STATES))
        .order_by(desc(Post.created_at)).limit(posts)
    ).scalars().all()
    history = []
    for p in rows:
        extra = p.extra or {}
        trace = extra.get("format_trace") or {}
        retargeted = extra.get("retargeted") or {}
        history.append({
            "id": p.id,
            "at": (p.published_at or p.scheduled_at or p.created_at),
            "format": p.format,
            "state": p.state,
            "title": (p.article.title if p.article else "")[:70],
            "notes": list(extra.get("format_notes") or []),
            "decision": trace.get("decision", ""),
            "blocked": trace.get("blocked") or {},
            "ai_choice": trace.get("ai_choice", ""),
            "retargeted": (f"{retargeted.get('from')} → {p.format}: FB kartīte nogrieztu "
                           f"{float(retargeted.get('link_card_crop') or 0) * 100:.0f}%"
                           if retargeted else ""),
        })
    from app import slots

    upcoming = session.execute(
        select(Post).where(Post.channel == name, Post.state == "scheduled",
                           Post.scheduled_at.is_not(None), Post.scheduled_at > utcnow())
        .order_by(Post.scheduled_at)
    ).scalars().all()
    now = utcnow()
    queue_plan = {
        "waiting": len(upcoming),
        "gap_now_minutes": int(slots.adaptive_gap(cfg, len(upcoming)).total_seconds() // 60),
        "gap_base_minutes": int(cfg.get("min_gap_minutes", 30)),
        "gap_floor_minutes": int(slots.gap_floor(cfg).total_seconds() // 60),
        "next": [{"id": p.id, "at": p.scheduled_at, "format": p.format,
                  "title": (p.article.title if p.article else "")[:60],
                  "priority": round(slots.priority(p, now), 3),
                  "movable": slots.movable(p, now),
                  "replanned": bool((p.extra or {}).get("replanned"))}
                 for p in upcoming[:12]],
    }
    return {
        "channel": name,
        "display_name": cfg.get("display_name", name),
        "queue_plan": queue_plan,
        "formats": allowed,
        "weights": weights,
        "floors": floors,
        "ceilings": ceilings,
        "caps": caps,
        "row_limit": formats.row_limit(cfg),
        "window": formats.recent_formats(session, name),
        "shares": {f: round(v, 3) for f, v in shares.items()},
        "run": {"format": head, "count": run},
        "today": today,
        "status": status,
        "history": history,
    }


def simulate(session, name: str, cfg: dict, article: Article) -> list[dict]:
    """Ko sistēma izvēlētos ŠOBRĪD šim rakstam pie katras AI izvēles."""
    out = []
    for ai_choice in ("", "link", "photo", "card_carousel", "reel"):
        if ai_choice and ai_choice not in (cfg.get("formats") or []):
            continue
        trace = formats.explain(session, name, cfg, article, ai_choice or None)
        row = {"ai_choice": ai_choice or "—", "chosen": trace["chosen"],
               "decision": trace.get("decision", ""), "note": trace.get("note", ""),
               "blocked": trace.get("blocked") or {},
               "scores": trace.get("scores") or {}}
        if ai_choice in pipeline.RICH_FORMATS:
            row["gate"] = pipeline.rich_format_gate(session, name, cfg, article, ai_choice)
        out.append(row)
    return out


def _video_summary(session) -> dict:
    from app import videos

    try:
        return videos.summary(session)
    except Exception as e:  # noqa: BLE001 — diagnostika nedrīkst krist
        return {"error": str(e)[:200]}


def _play_summary(session) -> dict:
    from app import play

    try:
        return play.summary(session)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}


def report(session, channel: str = "", posts: int = 15,
           simulate_article: bool = False) -> dict:
    """Visa diagnostika vienā struktūrā (lapai, eksportam un skriptam)."""
    from app import cards

    channels = config.load_channels()
    if channel:
        channels = {k: v for k, v in channels.items() if k == channel}
    since = utcnow() - timedelta(hours=24)
    published = session.execute(
        select(Post).where(Post.state == "published", Post.published_at >= since)
    ).scalars().all()
    mix: dict[str, int] = {}
    for p in published:
        mix[p.format] = mix.get(p.format, 0) + 1

    data = {
        "generated_at": utcnow(),
        "rules_dir": str(config.RULES_DIR),
        "editable_rules_used": config.RULES_DIR != config.DEFAULT_RULES_DIR,
        "rule_drift": config.rule_drift(),
        "renderer_available": cards.renderer_available(),
        "reels_available": _reels_available(),
        "last_render_error": cards.last_render_failure(),
        "ads_mode": get_setting(session, "ads:mode", "off"),
        "published_24h": mix,
        "queue": _queue_health(session),
        "reel_voice": _reel_voice(session),
        "ai_cost": _ai_cost(session),
        "video_archive": _video_summary(session),
        "play": _play_summary(session),
        "channels": [channel_diagnostics(session, name, cfg or {}, posts)
                     for name, cfg in channels.items()],
        "ads": _ads_summary(session),
    }
    if simulate_article:
        art = session.execute(
            select(Article).where(Article.title != "", Article.images.is_not(None))
            .order_by(desc(Article.first_seen_at)).limit(1)).scalars().first()
        if art is not None:
            data["simulation"] = {
                "article": art.title,
                "channels": {name: simulate(session, name, cfg or {}, art)
                             for name, cfg in channels.items()},
            }
    return data


# Orientējošas cenas ($ par 1M žetonu). Kešā lasītā ievade maksā ~10 %.
# Tas ir aplēses rīks lēmumam «vai mainīt modeli», ne grāmatvedība: īsto
# rēķinu rāda Console. Bez šī modeļa maiņas cena ir sajūta, ne skaitlis.
MODEL_PRICES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def _estimate_usd(model: str, fresh: int, cached: int, out: int) -> float | None:
    price = next((v for k, v in MODEL_PRICES.items() if str(model).startswith(k)), None)
    if price is None:
        return None
    inp, outp = price
    return round(fresh / 1e6 * inp + cached / 1e6 * inp * 0.1 + out / 1e6 * outp, 3)


def _ai_cost(session, hours: int = 72) -> dict:
    """Cik Claude izsaukumu un cik žetonu pēdējā diennaktī, pa modeļiem.

    Bez šī «API tērē daudz» ir sajūta, ne skaitlis: nevar pateikt, vai maksā
    izsaukumu skaits, prompta garums vai kešs, kas nestrādā.
    """
    since = utcnow() - timedelta(hours=hours)
    rows = session.execute(
        select(DecisionLog).where(DecisionLog.created_at >= since)).scalars().all()
    by_model: dict[str, dict] = {}
    for r in rows:
        cur = by_model.setdefault(r.model or "?", {"calls": 0, "in": 0, "out": 0, "cached": 0})
        cur["calls"] += 1
        cur["in"] += int(r.input_tokens or 0)
        cur["out"] += int(r.output_tokens or 0)
        cur["cached"] += int(getattr(r, "cached_tokens", 0) or 0)
    for name, m in by_model.items():
        m["usd"] = _estimate_usd(name, m["in"], m["cached"], m["out"])
    reused = sum(1 for r in rows if int(getattr(r, "reused", 0) or 0))
    # Pa dienām un pa plūsmām: «kāpēc tieši 4. septembrī» citādi ir jautājums,
    # uz kuru var atbildēt tikai Console diagramma, un tā nezina, KAS šeit
    # tērēja — ziņas, Play katalogs vai video arhīvs.
    feeds = dict(session.execute(select(Article.id, Article.feed_name)).all())
    by_day: dict[str, dict] = {}
    by_feed: dict[str, dict] = {}
    for r in rows:
        if int(getattr(r, "reused", 0) or 0):
            continue
        billed = int(r.input_tokens or 0) + int(getattr(r, "cached_tokens", 0) or 0)
        day = by_day.setdefault(r.created_at.date().isoformat(),
                                {"calls": 0, "input": 0, "output": 0})
        day["calls"] += 1
        day["input"] += billed
        day["output"] += int(r.output_tokens or 0)
        feed = by_feed.setdefault(feeds.get(r.article_id) or "?",
                                  {"calls": 0, "input": 0})
        feed["calls"] += 1
        feed["input"] += billed
    total_in = sum(m["in"] for m in by_model.values())
    total_cached = sum(m["cached"] for m in by_model.values())
    # API `input_tokens` NEIETVER kešoto daļu — tā nāk atsevišķi. Dalot ar
    # `input_tokens`, kešs izskatījās daudzkārt sliktāks, nekā ir patiesībā.
    billed = total_in + total_cached
    return {"hours": hours, "calls": len(rows) - reused, "reused": reused,
            "input": total_in, "output": sum(m["out"] for m in by_model.values()),
            "cached": total_cached, "total_input": billed,
            "cache_pct": round(100 * total_cached / billed) if billed else 0,
            "by_model": dict(sorted(by_model.items(), key=lambda kv: -kv[1]["calls"])),
            "usd": round(sum(m["usd"] or 0 for m in by_model.values()), 2),
            "by_day": dict(sorted(by_day.items())),
            "by_feed": dict(sorted(by_feed.items(), key=lambda kv: -kv[1]["input"]))}


def _reel_voice(session, hours: int = 48) -> dict:
    """Vai publicētajās lentēs tiešām ir skaņa — un kad nav, tad kāpēc.

    Sintēze nemet kļūdu: neizdevusies ieruna nozīmē klusu lenti, un klusa
    lente izskatās gluži kā apzināta izvēle. Bez šī bloka «kāpēc dažām lentēm
    nav skaņas» nav atbildams ne redaktoram, ne izstrādātājam.
    """
    since = utcnow() - timedelta(hours=hours)
    rows = session.execute(
        select(Post).where(Post.format == "reel", Post.created_at >= since)
    ).scalars().all()
    out = {"hours": hours, "reels": len(rows), "voiced": 0, "silent": [],
           "reasons": {}, "voices": {}}
    try:
        from app import tts

        out["configured"] = tts.configured_voices()
    except Exception:  # noqa: BLE001 — diagnostika nedrīkst gāzt lapu
        out["configured"] = {}
    for p in rows:
        recipe = (p.extra or {}).get("recipe") or {}
        if recipe.get("voiced"):
            out["voiced"] += 1
            used = str(recipe.get("voice_used") or "")
            if used:
                out["voices"][used] = out["voices"].get(used, 0) + 1
            continue
        if recipe.get("kind") == "video_reel":
            why = "avota klipam nav skaņas celiņa"
        elif recipe.get("voice_errors"):
            why = "; ".join(recipe["voice_errors"])
        elif "voice_errors" not in recipe:
            # Lente būvēta pirms kļūdu uzskaites. To NEDRĪKST pasniegt kā
            # diagnozi: nezināms iemesls un «nav atslēgas» ir divas dažādas
            # lietas, un minējums te maksātu dienu nepareizas meklēšanas.
            why = "iemesls nav pierakstīts (būvēta pirms šīs uzskaites)"
        else:
            why = "sintēze atgrieza tukšu audio bez kļūdas"
        out["silent"].append({"post": p.id, "section": recipe.get("section", ""),
                              "why": why[:200]})
        out["reasons"][why[:120]] = out["reasons"].get(why[:120], 0) + 1
    return out


def _queue_health(session) -> dict:
    """Kāpēc nekas netiek plānots: lemjamā rinda un pēdējie sargu iemesli."""
    from app import pipeline

    try:
        return pipeline.queue_health(session)
    except Exception as e:  # noqa: BLE001 — diagnostika nedrīkst gāzt lapu
        return {"error": str(e)}


def _reels_available() -> bool:
    try:
        from app import reels

        return reels.available()
    except Exception:  # noqa: BLE001
        return False


def _ads_summary(session) -> list[dict]:
    """Formātu maksas rezultāti — tie paši skaitļi, ko lieto `ad_multipliers`."""
    rows = session.execute(
        select(Post.format, AdEntry.spent_cents, AdEntry.sessions, AdEntry.clicks)
        .join(Post, Post.id == AdEntry.post_id)
        .where(AdEntry.status.in_(("active", "paused", "done")))
    ).all()
    agg: dict[str, dict] = {}
    for fmt, spent, sessions, clicks in rows:
        cur = agg.setdefault(fmt, {"format": fmt, "ads": 0, "spent": 0,
                                   "sessions": 0, "clicks": 0})
        cur["ads"] += 1
        cur["spent"] += spent or 0
        cur["sessions"] += sessions or 0
        cur["clicks"] += clicks or 0
    for cur in agg.values():
        euros = cur["spent"] / 100
        result = cur["sessions"] or cur["clicks"]
        cur["eur"] = round(euros, 2)
        cur["per_eur"] = round(result / euros, 2) if euros else None
    return sorted(agg.values(), key=lambda r: -(r["per_eur"] or 0))
