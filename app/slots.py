"""Slot allocator: picks WHEN a post goes out on a channel.

Enforces (per channel): minimum gap, daily cap, quiet hours, sensitivity
time windows, similarity guard, and section/format diversity. Prefers
hours where the audience is historically active (DEFAULT_HOUR_WEIGHTS,
replaced by measured priors in Phase 3).
"""
from __future__ import annotations

from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app import config
from app.best_practices import DEFAULT_HOUR_WEIGHTS
from app.models import Post
from app.rules_engine import Verdict, in_window, parse_window, title_similarity

STEP_MINUTES = 5
SEARCH_HORIZON_HOURS = 48


def _local(dt_utc: datetime) -> datetime:
    return dt_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(config.TIMEZONE))


def _channel_queue(session, channel: str, around: datetime) -> list[Post]:
    """Scheduled/published posts near the search window, oldest first."""
    lo = around - timedelta(hours=26)
    hi = around + timedelta(hours=SEARCH_HORIZON_HOURS + 1)
    rows = session.execute(
        select(Post)
        .where(Post.channel == channel,
               Post.state.in_(("scheduled", "publishing", "published")),
               Post.scheduled_at.is_not(None),
               Post.scheduled_at >= lo, Post.scheduled_at <= hi)
        .order_by(Post.scheduled_at)
    ).scalars().all()
    return list(rows)


def _quiet(local_dt: datetime, quiet_hours: list[str]) -> bool:
    t = local_dt.time()
    return any(in_window(t, *parse_window(w)) for w in quiet_hours)


def _window_ok(local_dt: datetime, windows: list[tuple[time, time]]) -> bool:
    if not windows:
        return True
    return any(in_window(local_dt.time(), a, b) for a, b in windows)


def _daily_count(queue: list[Post], local_day, tz) -> int:
    return sum(1 for p in queue
               if p.scheduled_at and _local(p.scheduled_at).date() == local_day)


def violates_similarity(session, channel: str, title: str, rules: dict,
                        queue: list[Post] | None = None,
                        slot: datetime | None = None) -> bool:
    """True when the title repeats one of the posts right before `slot`
    (or the latest posts when no slot is given). Judging per candidate slot
    matters: a near-duplicate blocks the next slots, not the whole day."""
    guard = rules.get("similarity_guard") or {}
    window = int(guard.get("window", 3))
    threshold = float(guard.get("threshold", 0.70))
    if queue is None:
        queue = []
    posts = [p for p in queue if p.scheduled_at
             and (slot is None or p.scheduled_at <= slot)]
    recent = sorted(posts, key=lambda p: p.scheduled_at)[-window:]
    for p in recent:
        other = p.article.title if p.article else p.copy
        if title_similarity(title, other) >= threshold:
            return True
    return False


def violates_diversity(queue: list[Post], candidate_section: str, candidate_format: str,
                       slot: datetime, rules: dict,
                       channel_cfg: dict | None = None) -> bool:
    """Within the rolling window ending at this slot, require a section+format mix.
    Requirements are capped by what the channel can actually produce — a
    single-format channel (e.g. stories) can't be asked for two formats."""
    mix = rules.get("section_mix") or {}
    window = int(mix.get("window", 6))
    min_sections = int(mix.get("min_distinct_sections", 2))
    min_formats = int(mix.get("min_distinct_formats", 2))
    if channel_cfg:
        fmts = channel_cfg.get("formats") or []
        if fmts:
            min_formats = min(min_formats, len(fmts))
        secs = channel_cfg.get("sections") or []
        if secs:
            min_sections = min(min_sections, len(secs))
    prior = sorted((p for p in queue if p.scheduled_at and p.scheduled_at <= slot),
                   key=lambda p: p.scheduled_at)[-(window - 1):]
    if len(prior) < window - 1:
        return False  # not enough history to constrain
    sections = {p.article.section if p.article else "" for p in prior} | {candidate_section}
    formats = {p.format for p in prior} | {candidate_format}
    return len(sections) < min_sections or len(formats) < min_formats


def find_slot(session, channel: str, channel_cfg: dict, verdict: Verdict,
              section: str, fmt: str, title: str,
              now: datetime, preferred: datetime | None = None,
              score: float = 0.0) -> datetime | None:
    return plan_slot(session, channel, channel_cfg, verdict, section, fmt,
                     title, now, preferred, score)[0]


def plan_slot(session, channel: str, channel_cfg: dict, verdict: Verdict,
              section: str, fmt: str, title: str,
              now: datetime, preferred: datetime | None = None,
              score: float = 0.0) -> tuple[datetime | None, str]:
    """(slot, reason) — earliest valid slot honouring all constraints, and
    when nothing fits, which guard actually blocked it.

    Among the first valid candidates we bias toward high-engagement hours:
    we scan forward and accept a slot immediately if its hour weight is
    within 85% of the best weight seen in the next 3 hours — so posts go
    out promptly but drift toward strong hours when the queue allows.
    """
    rules = config.load_rules()
    tz = ZoneInfo(config.TIMEZONE)
    queue = _channel_queue(session, channel, now)
    blocked_by: dict[str, int] = {}

    # measured audience curve replaces the default once enough data exists
    from app import priors

    hour_weights = priors.channel_hour_weights(session, channel) or DEFAULT_HOUR_WEIGHTS

    min_gap = timedelta(minutes=int(channel_cfg.get("min_gap_minutes", 30)))
    daily_cap = int(channel_cfg.get("daily_cap", 24))
    # the cap is soft: strong content may exceed it (min_gap remains the
    # real anti-flood guard) — rules.yaml daily_cap_flex
    flex = rules.get("daily_cap_flex") or {}
    if score >= float(flex.get("min_score", 1.01)):
        daily_cap = int(daily_cap * float(flex.get("max_factor", 1.0)))
    quiet_hours = channel_cfg.get("quiet_hours") or []

    start = max(now, verdict.earliest or now, preferred or now)
    # round up to the next 5-minute mark
    start = start.replace(second=0, microsecond=0)
    if start.minute % STEP_MINUTES:
        start += timedelta(minutes=STEP_MINUTES - start.minute % STEP_MINUTES)

    def valid(candidate: datetime) -> bool:
        def no(reason: str) -> bool:
            blocked_by[reason] = blocked_by.get(reason, 0) + 1
            return False

        if verdict.latest and candidate > verdict.latest:
            return no("statusa termiņš")
        local_dt = candidate.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
        if _quiet(local_dt, quiet_hours):
            return no("klusās stundas")
        if not _window_ok(local_dt, verdict.allowed_windows):
            return no("jutīga satura laika logs")
        for p in queue:
            if p.scheduled_at and abs(p.scheduled_at - candidate) < min_gap:
                return no("minimālā atstarpe — rinda pilna")
        if _daily_count(queue, local_dt.date(), tz) >= daily_cap:
            return no("dienas limits")
        if violates_diversity(queue, section, fmt, candidate, rules, channel_cfg):
            return no("daudzveidība (sadaļu/formātu mikss)")
        if violates_similarity(session, channel, title, rules, queue, candidate):
            return no("līdzīgs ieraksts tuvu šim laikam")
        return True

    if verdict.outcome == "forced_now":
        # 'now' skips cadence/caps/quiet hours, but several simultaneous
        # 'now' items still get spaced out so the channel isn't flooded.
        burst_gap = timedelta(minutes=int(rules.get("now_burst_gap_minutes", 5)))
        candidate = max(now, verdict.earliest or now)
        taken = sorted(p.scheduled_at for p in queue if p.scheduled_at)
        while any(abs(t - candidate) < burst_gap for t in taken):
            candidate += burst_gap
        return candidate, ""

    best: datetime | None = None
    best_weight = -1.0
    candidate = start
    end = start + timedelta(hours=SEARCH_HORIZON_HOURS)
    while candidate <= end:
        if verdict.latest and candidate > verdict.latest:
            break
        if valid(candidate):
            hour = candidate.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).hour
            weight = hour_weights.get(hour, 0.5)
            if best is None:
                best, best_weight = candidate, weight
            # look ahead up to 3h for a clearly better hour
            elif candidate - best <= timedelta(hours=3) and weight > best_weight / 0.85:
                best, best_weight = candidate, weight
            elif candidate - best > timedelta(hours=3):
                break
        candidate += timedelta(minutes=STEP_MINUTES)

    if best is None and verdict.latest:
        # 'must' deadline at risk: take literally any slot that only respects the gap
        candidate = start
        while candidate <= verdict.latest:
            if all(not (p.scheduled_at and abs(p.scheduled_at - candidate) < min_gap)
                   for p in queue):
                return candidate, ""
            candidate += timedelta(minutes=STEP_MINUTES)
    if best is not None:
        return best, ""
    top = sorted(blocked_by.items(), key=lambda kv: -kv[1])[:2]
    reason = ", ".join(name for name, _ in top) or "nav derīga laika"
    return None, reason
