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
from app.models import AdEntry, Article, Post, get_setting, utcnow

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
        "renderer_available": cards.renderer_available(),
        "reels_available": _reels_available(),
        "last_render_error": cards.last_render_failure(),
        "ads_mode": get_setting(session, "ads:mode", "off"),
        "published_24h": mix,
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
