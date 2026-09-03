"""Deterministic rule engine. Runs before the AI and always wins.

For each (article, channel) it returns one of:
  eligible      — AI may decide, within the given time constraints
  blocked       — never post (with a human-readable reason)
  forced_now    — editor said 'now': publish within minutes

Cadence (min gap, daily cap) and diversity are enforced at slot allocation
time (slots.py) because they depend on what else is in the queue.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app import config
from app.models import Article


@dataclass
class Verdict:
    outcome: str                       # eligible | blocked | forced_now
    reason: str = ""
    earliest: datetime | None = None   # UTC-naive, like everything in the DB
    latest: datetime | None = None     # statusa termiņš ("must"); atmetams
    # Satura derīguma termiņš: brīdis, kad raksts kļūst par vecu, lai to
    # vispār publicētu (max_age_hours). ATŠĶIRAS no `latest`: statusa termiņš
    # ir mūsu solījums redakcijai un pilnas rindas gadījumā to drīkst atmest,
    # bet svaigums ir paša satura īpašība — vakardienas ziņa rīt nekļūst
    # svaigāka. None = evergreen vai sadaļai limita nav.
    fresh_until: datetime | None = None
    allowed_windows: list[tuple[time, time]] = field(default_factory=list)


def _tz() -> ZoneInfo:
    return ZoneInfo(config.TIMEZONE)


def parse_window(s: str) -> tuple[time, time]:
    a, b = s.split("-")
    ah, am = a.split(":")
    bh, bm = b.split(":")
    return time(int(ah), int(am)), time(int(bh), int(bm))


def in_window(t: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= t < end
    return t >= start or t < end  # overnight window, e.g. 22:00-06:00


def local_time(dt_utc: datetime) -> time:
    return dt_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(_tz()).time()


def article_age_hours(article: Article, now: datetime) -> float:
    ref = article.published_at or article.first_seen_at
    return max(0.0, (now - ref).total_seconds() / 3600)


def title_similarity(a: str, b: str) -> float:
    """Token Jaccard over lowercased words — cheap, no external model needed."""
    wa = set(re.findall(r"\w{3,}", a.lower()))
    wb = set(re.findall(r"\w{3,}", b.lower()))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def evaluate(article: Article, channel_name: str, channel_cfg: dict,
             rules: dict, now: datetime) -> Verdict:
    status = article.editor_status

    # 1. Hard editor overrides
    if status == "dont":
        return Verdict("blocked", "editor status: don't publish")

    # 2. Channel scoping
    sections = channel_cfg.get("sections") or []
    if sections and article.section not in sections:
        return Verdict("blocked", f"section '{article.section}' not routed to this channel")

    blocklist = (rules.get("term_blocklist") or {}).get(channel_name) or []
    if blocklist and any(str(t) in {str(b) for b in blocklist} for t in article.term_ids):
        return Verdict("blocked", "term id on channel blocklist")

    allowlist = (rules.get("term_allowlist") or {}).get(channel_name) or []
    if allowlist and not any(str(t) in {str(a) for a in allowlist} for t in article.term_ids):
        return Verdict("blocked", "term id not on channel allowlist")

    # 3. 'now' skips everything below — but only while genuinely fresh.
    # A backlog of old 'now' items (e.g. the first ingest of a /now/ feed)
    # must not flood the channels; stale ones fall through to normal rules.
    if status == "now":
        max_now_age = float(rules.get("now_max_age_hours", 6))
        if article_age_hours(article, now) <= max_now_age:
            minutes = int(rules.get("now_max_delay_minutes", 5))
            return Verdict("forced_now", "editor status: publish now",
                           earliest=now, latest=now + timedelta(minutes=minutes))

    # 4. Freshness
    max_age = (rules.get("max_age_hours") or {}).get(article.section)
    if (article.raw_json or {}).get("_video"):
        # tv3.lv/video arhīva klips dzīvo ilgāk par ziņu (video_archive.max_age_hours)
        from app import videos

        max_age = videos.settings(rules).get("max_age_hours", 72)
    if (article.raw_json or {}).get("_play"):
        from app import play

        max_age = play.settings(rules).get("max_age_hours", 240)
    fresh_until = None
    if max_age is not None and article.editor_timeframe != "evergreen":
        if article_age_hours(article, now) > float(max_age):
            return Verdict("blocked",
                           f"too old: {article_age_hours(article, now):.0f}h > {max_age}h "
                           f"limit for {article.section}")
        # Cik ilgi raksts paliek publicējams. Līdz šim svaigumu pārbaudīja
        # TIKAI lēmuma brīdī, un slots pēc tam varēja aizceļot 48 h uz priekšu:
        # šodienas ziņa tad iznāca kā parīta stāsts.
        ref = article.published_at or article.first_seen_at
        fresh_until = ref + timedelta(hours=float(max_age))

    # 5. Sensitivity time windows
    windows: list[tuple[time, time]] = []
    for rule in rules.get("time_windows") or []:
        match_sens = set((rule.get("match") or {}).get("sensitivity") or [])
        if match_sens & set(article.sensitivity or []):
            if status in (rule.get("unless_status") or []):
                continue
            windows.extend(parse_window(w) for w in rule.get("allow") or [])

    if (article.raw_json or {}).get("_play"):
        # TV3 Play promo tikai vakara logā; 16+/18+ vēl vēlāk
        from app import play

        windows.extend(parse_window(w) for w in play.windows_for(article, rules))

    # 6. Deadline for 'must'
    latest = None
    if status == "must":
        hours = float(rules.get("must_max_delay_hours", 6))
        latest = now + timedelta(hours=hours)

    return Verdict("eligible",
                   "must publish within deadline" if status == "must" else "AI decides",
                   earliest=now, latest=latest, fresh_until=fresh_until,
                   allowed_windows=windows)


def evaluate_all(article: Article, now: datetime) -> dict[str, Verdict]:
    """Evaluate an article against every configured channel."""
    rules = config.load_rules()
    channels = config.load_channels()
    return {
        name: evaluate(article, name, cfg or {}, rules, now)
        for name, cfg in channels.items()
    }
