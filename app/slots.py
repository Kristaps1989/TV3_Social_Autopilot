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

# Cik ātri ziņa zaudē vērtību: pēc katra pusperioda tās vērtība dalās uz pusi.
# Sporta rezultāts nākamajā rītā ir nevērtīgs; skaidrojums vai izklaide tur
# vērtību dienām. Noteikumos: section_half_life_hours.
DEFAULT_HALF_LIFE_HOURS = {"news": 4.0, "sport": 2.0, "entertainment": 24.0}
# Cik stundās rindai jāiztukšojas — no tā izriet atstarpe starp ierakstiem,
# kad rinda ir dziļa. Noteikumos: backlog_horizon_hours.
DEFAULT_BACKLOG_HORIZON_HOURS = 2.0
# Šaurākā pieļaujamā atstarpe pa platformām, kad rinda ir pilna (kanālā:
# min_gap_floor_minutes). Facebook plūsma ir ranžēta, ne hronoloģiska — divi
# ieraksti 15 min attālumā nekonkurē; lielie ziņu konti tā strādā visu dienu.
DEFAULT_GAP_FLOOR_MINUTES = {"facebook_page": 15, "x": 10, "threads": 15,
                             "instagram": 30}


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


def _daily_count_section(queue: list[Post], local_day, section: str) -> int:
    return sum(1 for p in queue
               if p.scheduled_at and _local(p.scheduled_at).date() == local_day
               and (p.article.section if p.article else "") == section)


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


def gap_floor(channel_cfg: dict) -> timedelta:
    """Šaurākā atstarpe, līdz kurai rinda drīkst saspiesties. Viena formāta
    stāstu kanālam adaptācijas nav: tur atstarpe ir nevis pret pārplūdi, bet
    pret to, ka stāsti ir piedeva, ne otra plūsma."""
    base = int(channel_cfg.get("min_gap_minutes", 30))
    if channel_cfg.get("min_gap_floor_minutes") is not None:
        return timedelta(minutes=max(1, min(base, int(channel_cfg["min_gap_floor_minutes"]))))
    if list(channel_cfg.get("formats") or []) == ["story"]:
        return timedelta(minutes=base)
    floor = DEFAULT_GAP_FLOOR_MINUTES.get(channel_cfg.get("platform", ""), base)
    return timedelta(minutes=min(base, floor))


def adaptive_gap(channel_cfg: dict, waiting: int, rules: dict | None = None) -> timedelta:
    """Atstarpe starp ierakstiem, kas atkarīga no rindas dziļuma.

    Fiksēta atstarpe (45 min) ir pareiza tukšai rindai: tur tā sargā no
    pārplūdes. Bet vakara ziņu vilnī tā pati atstarpe ziņas sarindo līdz
    nākamajam rītam, un rīta plūsmā iziet vakardienas ziņas. Tāpēc atstarpe
    saraujas, kad rinda aug: mērķis ir iztukšot rindu `backlog_horizon_hours`
    laikā, bet ne šaurāk par `min_gap_floor_minutes`.
    """
    rules = config.load_rules() if rules is None else rules
    base = timedelta(minutes=int(channel_cfg.get("min_gap_minutes", 30)))
    floor = gap_floor(channel_cfg)
    if floor >= base or waiting <= 0:
        return base
    horizon = timedelta(hours=float(rules.get("backlog_horizon_hours",
                                              DEFAULT_BACKLOG_HORIZON_HOURS)))
    wanted = horizon / (waiting + 1)
    return max(floor, min(base, wanted))


def half_life_hours(section: str, rules: dict | None = None) -> float:
    rules = config.load_rules() if rules is None else rules
    table = {**DEFAULT_HALF_LIFE_HOURS, **(rules.get("section_half_life_hours") or {})}
    return float(table.get(section) or table.get("news") or 4.0)


def priority(post: Post, now: datetime, rules: dict | None = None) -> float:
    """Cik vērtīgs ieraksts ir TAGAD: redaktora statuss un AI vērtējums,
    dalīti uz pusi ik pēc sadaļas pusperioda. Rindas kārtību nosaka šis, ne
    tas, kurš raksts ienāca pirmais."""
    article = post.article
    status = getattr(article, "editor_status", "can") if article else "can"
    base = {"now": 3.0, "must": 2.0}.get(status, 1.0)
    base += float(getattr(article, "ai_score", 0) or 0)
    if article is None:
        return base
    ref = article.published_at or article.first_seen_at or now
    age = max(0.0, (now - ref).total_seconds() / 3600)
    if (article.raw_json or {}).get("_play"):
        from app import play as _play

        hl = float(_play.settings(rules).get("half_life_hours") or 72)
    elif (article.raw_json or {}).get("_video"):
        # arhīva klips nav ziņa: tas aizpilda tukšumus, nevis cīnās par vietu
        from app import videos

        hl = float(videos.settings(rules).get("half_life_hours") or 48)
    else:
        hl = half_life_hours(article.section or "news", rules)
    return base * 0.5 ** (age / hl)


def _quiet_exempt(section: str, score: float, age_hours: float | None,
                  rules: dict) -> bool:
    """Kas drīkst iziet klusajās stundās: sporta rezultāts pēc vakara mača
    un ļoti spēcīga ziņa — abi rīt no rīta būtu vakardiena."""
    cfg = rules.get("quiet_hours_exempt")
    if cfg is None:
        cfg = {"min_score": 0.85, "sections": ["sport"], "max_age_hours": 2}
    if not cfg:
        return False
    if age_hours is not None and age_hours > float(cfg.get("max_age_hours", 2)):
        return False
    return (section in (cfg.get("sections") or [])
            or score >= float(cfg.get("min_score", 1.01)))


def find_slot(session, channel: str, channel_cfg: dict, verdict: Verdict,
              section: str, fmt: str, title: str,
              now: datetime, preferred: datetime | None = None,
              score: float = 0.0) -> datetime | None:
    return plan_slot(session, channel, channel_cfg, verdict, section, fmt,
                     title, now, preferred, score)[0]


def plan_slot(session, channel: str, channel_cfg: dict, verdict: Verdict,
              section: str, fmt: str, title: str,
              now: datetime, preferred: datetime | None = None,
              score: float = 0.0,
              allow_similar: bool = False,
              pending: int = 0,
              age_hours: float | None = None,
              promo: bool = False) -> tuple[datetime | None, str]:
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

    # asap (default): news-portal mode — first valid slot wins, freshness
    # beats hour optimisation and the diversity guard never delays content.
    # optimize: drift toward measured strong hours + slot-level diversity
    # (flip in rules.yaml scheduling_mode once GA4 data justifies it).
    asap = str(rules.get("scheduling_mode", "asap")).lower() != "optimize"
    # atstarpe saraujas, kad rinda ir dziļa: skaita gaidošos ierakstus pēc
    # `now` plus tos, kas vēl tiks likti šajā pašā plānošanā (`pending`)
    waiting = sum(1 for p in queue
                  if p.scheduled_at and p.scheduled_at > now and p.state == "scheduled")
    min_gap = adaptive_gap(channel_cfg, waiting + pending, rules)
    daily_cap = int(channel_cfg.get("daily_cap") or 0)  # 0/missing = unlimited
    # the cap is soft: strong content may exceed it (min_gap remains the
    # real anti-flood guard) — rules.yaml daily_cap_flex
    flex = rules.get("daily_cap_flex") or {}
    if daily_cap and score >= float(flex.get("min_score", 1.01)):
        daily_cap = int(daily_cap * float(flex.get("max_factor", 1.0)))
    per_section_cap = int(channel_cfg.get("daily_cap_per_section") or 0)
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
        # Svaigums ir paša satura īpašība, ne mūsu solījums: pilna rinda to
        # neatceļ. Bez šī slots aizceļoja līdz 48 h uz priekšu, un šodienas
        # ziņa iznāca kā parīta stāsts.
        if verdict.fresh_until and candidate > verdict.fresh_until:
            return no("saturs līdz tam būs par vecu")
        local_dt = candidate.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
        if (_quiet(local_dt, quiet_hours)
                and not _quiet_exempt(section, score, age_hours, rules)):
            return no("klusās stundas")
        if not _window_ok(local_dt, verdict.allowed_windows):
            return no("jutīga satura laika logs" if not promo else "Play vakara logs")
        if promo:
            # izklaides promo nekad blakus traģēdijai vai noziegumam
            from app import play as _play

            why_close = _play.too_close_to_grim(queue, candidate, rules)
            if why_close:
                return no(why_close)
        for p in queue:
            if p.scheduled_at and abs(p.scheduled_at - candidate) < min_gap:
                return no("minimālā atstarpe — rinda pilna")
        if daily_cap and _daily_count(queue, local_dt.date(), tz) >= daily_cap:
            return no("dienas limits")
        if per_section_cap and _daily_count_section(
                queue, local_dt.date(), section) >= per_section_cap:
            return no(f"sadaļas dienas limits ({section})")
        if not asap and violates_diversity(queue, section, fmt, candidate,
                                           rules, channel_cfg):
            return no("daudzveidība (sadaļu/formātu mikss)")
        if not allow_similar and violates_similarity(session, channel, title,
                                                     rules, queue, candidate):
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
            if asap:
                return candidate, ""  # freshness first: publish when ready
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
        # deadline at risk: drop the optimisation-level guards, but never the
        # hard ones — gap, quiet hours and the daily/section caps still hold
        candidate = start
        while candidate <= verdict.latest:
            local_dt = candidate.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
            if (all(not (p.scheduled_at and abs(p.scheduled_at - candidate) < min_gap)
                    for p in queue)
                    and not _quiet(local_dt, quiet_hours)
                    and not (daily_cap and _daily_count(queue, local_dt.date(), tz)
                             >= daily_cap)
                    and not (per_section_cap and _daily_count_section(
                        queue, local_dt.date(), section) >= per_section_cap)):
                return candidate, ""
            candidate += timedelta(minutes=STEP_MINUTES)
    if best is not None:
        return best, ""
    top = sorted(blocked_by.items(), key=lambda kv: -kv[1])[:2]
    reason = ", ".join(name for name, _ in top) or "nav derīga laika"
    return None, reason


MOVABLE_STATES = ("scheduled",)


def movable(post: Post, now: datetime) -> bool:
    """Ko pārplānošana drīkst pārvietot: automātikas ieplānoto nākotni.
    Redaktora rokas darbu, franšīzes (timeless) un `now` ierakstus neaiztiek."""
    extra = post.extra or {}
    if post.state not in MOVABLE_STATES or not post.scheduled_at:
        return False
    if post.scheduled_at <= now:
        return False
    if extra.get("manual") or extra.get("timeless") or extra.get("forced_now"):
        return False
    return True


def replan_channel(session, channel: str, channel_cfg: dict,
                   now: datetime, rules: dict | None = None) -> dict:
    """Pārkārto kanāla rindu pēc vērtības un svaiguma, ne ienākšanas kārtas.

    Fiksēts logs («nākamais brīvais slots + 45 min») rindu veido FIFO: vakara
    ziņu vilnis sakrājas un iziet nākamajā rītā, un vēlāk ienākusi svarīgāka
    ziņa stāv aiz vājākām. Šeit gaidošos ierakstus sakārto pēc `priority`
    (statuss + AI vērtējums, dalīts uz pusi ik pēc sadaļas pusperioda) un
    sadala no jauna ar adaptīvo atstarpi. Kas līdz savam slotam būtu par
    vecu, tiek atcelts uzreiz — vecas ziņas publicēšana ir sliktāka par
    nepublicēšanu, un atbrīvotais slots aiziet svaigākai.

    Atgriež {"moved": n, "cancelled": n, "kept": n}.
    """
    rules = config.load_rules() if rules is None else rules
    if not rules.get("replan_queue", True):
        return {"moved": 0, "cancelled": 0, "kept": 0}
    from app.models import Article  # noqa: F401 — relācija ielādējas caur post.article

    rows = session.execute(
        select(Post).where(Post.channel == channel, Post.state == "scheduled",
                           Post.scheduled_at.is_not(None), Post.scheduled_at > now)
        .order_by(Post.scheduled_at)
    ).scalars().all()
    posts = [p for p in rows if movable(p, now)]
    if len(posts) < 2:
        return {"moved": 0, "cancelled": 0, "kept": len(posts)}

    order = sorted(posts, key=lambda p: -priority(p, now, rules))
    old = {p.id: p.scheduled_at for p in posts}
    # visi kustināmie iziet no rindas: plānotājs tad redz tikai nekustināmos
    for p in posts:
        p.scheduled_at = None
    session.flush()

    max_age = rules.get("max_age_hours") or {}
    moved = cancelled = kept = 0
    for i, p in enumerate(order):
        article = p.article
        extra = p.extra or {}
        fresh_until = None
        if article is not None and article.editor_timeframe != "evergreen":
            limit = max_age.get(article.section)
            ref = article.published_at or article.first_seen_at
            if limit is not None and ref is not None:
                fresh_until = ref + timedelta(hours=float(limit))
        latest = extra.get("latest")
        latest = datetime.fromisoformat(latest) if isinstance(latest, str) else None
        not_before = extra.get("not_before")
        not_before = (datetime.fromisoformat(not_before)
                      if isinstance(not_before, str) else None)
        verdict = Verdict("eligible", earliest=max(now, not_before or now),
                          latest=latest, fresh_until=fresh_until)
        age_hours = None
        if article is not None and (article.published_at or article.first_seen_at):
            age_hours = max(0.0, (now - (article.published_at or article.first_seen_at))
                            .total_seconds() / 3600)
        slot, why = plan_slot(session, channel, channel_cfg, verdict,
                              article.section if article else "", p.format,
                              article.title if article else (p.copy or ""),
                              now, None, score=float(getattr(article, "ai_score", 0) or 0),
                              allow_similar=True, pending=len(order) - i - 1,
                              age_hours=age_hours)
        if slot is None and fresh_until is not None and fresh_until <= now + timedelta(
                minutes=STEP_MINUTES):
            p.state = "cancelled"
            p.error = f"pārplānojot: raksts jau novecojis ({why or 'svaigums'})"
            p.scheduled_at = old[p.id]
            cancelled += 1
            session.flush()
            continue
        if slot is None:
            slot = old[p.id]      # nekas labāks — paliek, kur bija
        p.scheduled_at = slot
        if slot != old[p.id]:
            moved += 1
            p.extra = {**extra, "replanned": {"from": old[p.id].isoformat(),
                                             "priority": round(priority(p, now, rules), 3)}}
        else:
            kept += 1
        session.flush()
    session.commit()
    return {"moved": moved, "cancelled": cancelled, "kept": kept}
