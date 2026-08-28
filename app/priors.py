"""Performance priors: turn collected metrics into automatic improvements.

Score per post = GA4 sessions (the real KPI); platform clicks as fallback
while GA4 isn't wired; reactions count for nothing on their own.

Consumers:
  - slot allocator: measured hour-of-day curve replaces the default one
  - format chooser: measured sessions-per-post adjusts format weights
  - AI prompt: a short performance summary steers copy/format decisions
  - /stats page and the weekly report

Everything degrades to the configured defaults until enough data exists
(min sample sizes editable in rules.yaml -> priors:).
"""
from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app import config
from app.models import Post, PostMetrics, utcnow

DAYS = 30


def _min_samples() -> tuple[int, int]:
    p = (config.load_rules().get("priors") or {})
    return int(p.get("min_posts_hours", 30)), int(p.get("min_posts_format", 8))


def post_scores(session, channel: str | None = None, days: int = DAYS) -> list[dict]:
    """One row per published post with its best-known engagement score."""
    since = utcnow() - timedelta(days=days)
    metrics = session.execute(
        select(PostMetrics.post_id,
               func.max(PostMetrics.ga_sessions),
               func.max(PostMetrics.clicks),
               func.max(PostMetrics.impressions),
               func.max(PostMetrics.reactions))
        .group_by(PostMetrics.post_id)
    ).all()
    by_post = {m[0]: m for m in metrics}

    q = select(Post).where(Post.state == "published", Post.published_at >= since)
    if channel:
        q = q.where(Post.channel == channel)
    rows = []
    tz = ZoneInfo(config.TIMEZONE)
    for post in session.execute(q).scalars():
        m = by_post.get(post.id)
        ga, clicks, impressions, reactions = (m[1], m[2], m[3], m[4]) if m else (0, 0, 0, 0)
        score = float(ga if ga else clicks)
        local_hour = (post.published_at.replace(tzinfo=ZoneInfo("UTC"))
                      .astimezone(tz).hour if post.published_at else None)
        rows.append({
            "post": post, "score": score, "hour": local_hour,
            "format": post.format, "channel": post.channel,
            "section": post.article.section if post.article else "",
            "ga_sessions": ga, "clicks": clicks,
            "impressions": impressions, "reactions": reactions,
        })
    return rows


def channel_hour_weights(session, channel: str) -> dict[int, float] | None:
    """Measured hour-of-day engagement curve; None until the sample is big
    enough or nothing has been measured yet."""
    min_hours, _ = _min_samples()
    rows = [r for r in post_scores(session, channel) if r["hour"] is not None]
    if len(rows) < min_hours or not any(r["score"] > 0 for r in rows):
        return None
    by_hour: dict[int, list[float]] = {}
    for r in rows:
        by_hour.setdefault(r["hour"], []).append(r["score"])
    avg = {h: sum(v) / len(v) for h, v in by_hour.items()}
    peak = max(avg.values()) or 1.0
    global_mean = sum(avg.values()) / len(avg)
    # hours never posted in get a below-average weight, not zero — the
    # system keeps a little exploration instead of locking in early habits
    return {h: (avg.get(h, global_mean * 0.5)) / peak for h in range(24)}


def format_multipliers(session, channel: str) -> dict[str, float]:
    """Measured sessions-per-post per format, as a multiplier around 1.0
    applied on top of the configured format_weights. Formats without enough
    data get 1.0 (config weight stands)."""
    _, min_fmt = _min_samples()
    rows = post_scores(session, channel)
    by_fmt: dict[str, list[float]] = {}
    for r in rows:
        by_fmt.setdefault(r["format"], []).append(r["score"])
    scored = {f: sum(v) / len(v) for f, v in by_fmt.items() if len(v) >= min_fmt}
    if not scored or not any(v > 0 for v in scored.values()):
        return {}
    mean = sum(scored.values()) / len(scored) or 1.0
    return {f: min(2.0, max(0.3, v / mean)) for f, v in scored.items()}


def channel_summary(session, channel: str) -> dict:
    rows = post_scores(session, channel)
    n = len(rows)
    total_sessions = sum(r["ga_sessions"] for r in rows)
    total_clicks = sum(r["clicks"] for r in rows)
    fmts = {}
    for r in rows:
        fmts.setdefault(r["format"], []).append(r["score"])
    fmt_stats = sorted(
        ({"format": f, "n": len(v), "avg": (sum(v) / len(v)) if v else 0}
         for f, v in fmts.items()),
        key=lambda x: -x["avg"])
    hours = channel_hour_weights(session, channel)
    best_hours = ([h for h, _ in sorted(hours.items(), key=lambda kv: -kv[1])[:3]]
                  if hours else [])
    return {"channel": channel, "posts": n, "sessions": total_sessions,
            "clicks": total_clicks, "formats": fmt_stats, "best_hours": best_hours,
            "measured_curve": hours is not None}


def hook_summary(session, min_n: int = 4) -> list[dict]:
    """Cross-platform A/B result: avg score per hook style (per section),
    for hooks with at least min_n measured posts."""
    rows = post_scores(session)
    by_hook: dict[tuple[str, str], list[float]] = {}
    for r in rows:
        hook = (r["post"].hook_type or "").strip()
        if hook:
            by_hook.setdefault((r["section"] or "?", hook), []).append(r["score"])
    out = [{"section": sec, "hook": hook, "n": len(v), "avg": sum(v) / len(v)}
           for (sec, hook), v in by_hook.items() if len(v) >= min_n]
    out.sort(key=lambda x: (x["section"], -x["avg"]))
    return out


def top_posts(session, limit: int = 10) -> list[dict]:
    rows = post_scores(session)
    rows.sort(key=lambda r: -r["score"])
    return rows[:limit]


def prompt_context(session, channels: list[str]) -> str:
    """Short Latvian performance summary for the AI decision prompt."""
    lines = []
    for ch in channels:
        s = channel_summary(session, ch)
        if s["posts"] < 5:
            continue
        fmt_bits = ", ".join(f"{f['format']} vid. {f['avg']:.0f} klikšķi/sesijas (n={f['n']})"
                             for f in s["formats"][:3] if f["n"] >= 3)
        hour_bit = (" · stiprākās stundas: " + ", ".join(f"{h}:00" for h in s["best_hours"])
                    if s["best_hours"] else "")
        if fmt_bits or hour_bit:
            lines.append(f"- {ch}: {fmt_bits}{hour_bit}")
    hooks = hook_summary(session)
    if hooks:
        bits = ", ".join(f"{h['section']}/{h['hook']} vid. {h['avg']:.0f} (n={h['n']})"
                         for h in hooks[:6])
        lines.append(f"- āķu A/B rezultāti: {bits} — dod priekšroku uzvarētājiem,"
                     " bet turpini testēt pārējos")
    return "\n".join(lines)
