"""GA4 Data API collector: sessions/pageviews per post via utm_content.

Every outbound link carries utm_campaign=autopilot and utm_content=<post_id>
(see best_practices.add_utm), so one runReport call maps traffic back to
posts. Configured in the admin UI (Konti): GA4 property ID + a
service-account JSON with Analytics read access; env fallback:
GA4_PROPERTY_ID + GOOGLE_APPLICATION_CREDENTIALS (file path). Skips
silently when unconfigured.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import httpx

from app.models import Post, PostMetrics

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]


def property_id() -> str:
    from app import credentials

    return credentials.get("ga4_property_id")


def sa_info() -> dict | None:
    """Service-account JSON: admin UI (DB) first, env file path fallback."""
    from app import credentials

    raw = credentials.get("ga4_service_account")
    if not raw:
        path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        if not (path and Path(path).exists()):
            return None
        raw = Path(path).read_text(encoding="utf-8")
    try:
        info = json.loads(raw)
    except ValueError:
        log.warning("GA4 service-account JSON is not valid JSON")
        return None
    return info if info.get("client_email") and info.get("private_key") else None


def configured() -> bool:
    return bool(property_id() and sa_info())


def _token() -> str:
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_info(
        sa_info(), scopes=SCOPES)
    creds.refresh(Request())
    return creds.token


def collect(session, days: int = 3) -> int:
    """Pull sessions/pageviews by utm_content for recent days; store as
    PostMetrics rows. Returns number of posts updated."""
    if not configured():
        return 0
    prop = property_id()
    try:
        resp = httpx.post(
            f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport",
            headers={"Authorization": f"Bearer {_token()}"},
            json={
                "dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "today"}],
                "dimensions": [{"name": "sessionManualAdContent"}],
                "metrics": [{"name": "sessions"}, {"name": "screenPageViews"}],
                "dimensionFilter": {"filter": {
                    "fieldName": "sessionCampaignName",
                    "stringFilter": {"value": "autopilot"},
                }},
                "limit": 10000,
            },
            timeout=30,
        )
        resp.raise_for_status()
    except Exception as e:  # noqa: BLE001
        log.warning("GA4 collection failed: %s", e)
        return 0

    updated = 0
    for row in resp.json().get("rows", []):
        content = row["dimensionValues"][0]["value"]
        try:
            post_id = int(content)
        except (TypeError, ValueError):
            continue
        post = session.get(Post, post_id)
        if post is None:
            continue
        sessions_n = int(row["metricValues"][0]["value"] or 0)
        pageviews = int(row["metricValues"][1]["value"] or 0)
        session.add(PostMetrics(post_id=post_id, ga_sessions=sessions_n,
                                ga_pageviews=pageviews))
        updated += 1
    session.commit()
    return updated


# --- Site-wide dashboard (Portāls) ----------------------------------------
#
# Everything below reads the WHOLE tv3.lv property, not just autopilot
# traffic — a media-style overview dashboard (WoW/MoM/YoY, top content,
# traffic sources, section performance) built for editorial/strategic use.
# Comparisons are rolling windows (last N days vs the N days before that),
# not calendar-aligned months/years — simpler, no month-length edge cases,
# and labelled clearly in the UI as such.

DASHBOARD_METRICS = ["sessions", "activeUsers", "screenPageViews",
                     "averageSessionDuration", "engagementRate"]

_cache: dict[str, tuple[float, dict]] = {}
CACHE_TTL = 300.0  # seconds — avoid re-hitting the API on every page view


def _cached(key: str, build):
    import time

    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    value = build()
    _cache[key] = (now, value)
    return value


_last_error: str = ""


def last_error() -> str:
    """The most recent GA4 API failure, for the UI ('' when healthy)."""
    return _last_error


def _report(prop: str, body: dict) -> dict | None:
    global _last_error
    try:
        resp = httpx.post(
            f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport",
            headers={"Authorization": f"Bearer {_token()}"}, json=body, timeout=30)
        if resp.status_code != 200:
            log.warning("GA4 report failed %s: %s", resp.status_code, resp.text[:300])
            try:
                msg = resp.json().get("error", {}).get("message", "")
            except ValueError:
                msg = ""
            _last_error = f"HTTP {resp.status_code}: {msg or resp.text[:200]}"
            return None
        _last_error = ""
        return resp.json()
    except Exception as e:  # noqa: BLE001
        log.warning("GA4 report call failed: %s", e)
        _last_error = f"{type(e).__name__}: {str(e)[:250]}"
        return None


def _totals(prop: str, start: str, end: str, metrics: list[str]) -> dict[str, float] | None:
    data = _report(prop, {"dateRanges": [{"startDate": start, "endDate": end}],
                          "metrics": [{"name": m} for m in metrics]})
    if data is None:
        return None
    rows = data.get("rows") or []
    if not rows:
        return {m: 0.0 for m in metrics}
    vals = rows[0]["metricValues"]
    return {m: float(vals[i]["value"] or 0) for i, m in enumerate(metrics)}


def _window(days: int) -> tuple[str, str, str, str]:
    """(cur_start, cur_end, prev_start, prev_end) as GA4 relative date strings."""
    return (f"{days}daysAgo", "yesterday",
            f"{days * 2}daysAgo", f"{days + 1}daysAgo")


def _delta(cur: float, prev: float) -> dict:
    if prev > 0:
        pct = (cur - prev) / prev * 100
    elif cur > 0:
        pct = None  # went from zero -> something: no meaningful percentage
    else:
        pct = 0.0
    return {"current": cur, "previous": prev, "change_pct": pct}


def overview_windows(windows: tuple[int, ...] = (7, 30, 365)) -> dict:
    """{days: {metric: {current, previous, change_pct}}} for each window."""
    prop = property_id()
    out: dict[int, dict] = {}
    for days in windows:
        cs, ce, ps, pe = _window(days)
        cur = _totals(prop, cs, ce, DASHBOARD_METRICS)
        prev = _totals(prop, ps, pe, DASHBOARD_METRICS)
        if cur is None or prev is None:
            out[days] = {}
            continue
        out[days] = {m: _delta(cur[m], prev[m]) for m in DASHBOARD_METRICS}
    return out


def traffic_sources(days: int = 30, limit: int = 8) -> list[dict]:
    """Sessions by GA4's default channel grouping, with a WoW/period-over-
    period delta, sorted by current sessions descending."""
    prop = property_id()
    cs, ce, ps, pe = _window(days)

    def _by_channel(start: str, end: str) -> dict[str, float]:
        data = _report(prop, {
            "dateRanges": [{"startDate": start, "endDate": end}],
            "dimensions": [{"name": "sessionDefaultChannelGroup"}],
            "metrics": [{"name": "sessions"}],
            "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
            "limit": limit,
        })
        if data is None:
            return {}
        return {r["dimensionValues"][0]["value"]: float(r["metricValues"][0]["value"] or 0)
                for r in data.get("rows") or []}

    cur, prev = _by_channel(cs, ce), _by_channel(ps, pe)
    if not cur and not prev:
        return []
    total = sum(cur.values()) or 1.0
    rows = [{"channel": ch, "sessions": s, "pct": s / total * 100,
            **_delta(s, prev.get(ch, 0.0))}
           for ch, s in cur.items()]
    rows.sort(key=lambda r: -r["sessions"])
    return rows


def _page_metrics(start: str, end: str, limit: int) -> list[dict] | None:
    prop = property_id()
    data = _report(prop, {
        "dateRanges": [{"startDate": start, "endDate": end}],
        "dimensions": [{"name": "pagePath"}, {"name": "pageTitle"}],
        "metrics": [{"name": "screenPageViews"}, {"name": "sessions"},
                   {"name": "userEngagementDuration"}],
        "orderBys": [{"metric": {"metricName": "screenPageViews"}, "desc": True}],
        "limit": limit,
    })
    if data is None:
        return None
    out = []
    for r in data.get("rows") or []:
        path, title = r["dimensionValues"][0]["value"], r["dimensionValues"][1]["value"]
        views = float(r["metricValues"][0]["value"] or 0)
        sessions_n = float(r["metricValues"][1]["value"] or 0)
        engagement_s = float(r["metricValues"][2]["value"] or 0)
        out.append({"path": path, "title": title, "pageviews": views,
                   "sessions": sessions_n,
                   "avg_engagement_s": (engagement_s / views) if views else 0.0})
    return out


def top_content(days: int = 30, limit: int = 10) -> list[dict]:
    pages = _page_metrics(f"{days}daysAgo", "yesterday", limit=200)
    if not pages:
        return []
    from app import config

    url_sections = config.load_feeds().get("url_sections") or {}
    for p in pages:
        p["section"] = _section_for_path(p["path"], url_sections)
    return pages[:limit]


def _section_for_path(path: str, url_sections: dict) -> str:
    found = ""
    for seg in (path or "").strip("/").split("/"):
        if seg.lower() in url_sections:
            found = url_sections[seg.lower()]
    return found or "cits"


def section_breakdown(days: int = 30) -> list[dict]:
    """Sessions aggregated by CMS section (reusing rules/feeds.yaml
    url_sections — no GA4 custom dimension needed), with a period delta."""
    from app import config

    url_sections = config.load_feeds().get("url_sections") or {}
    if not url_sections:
        return []
    cs, ce, ps, pe = _window(days)
    cur_pages = _page_metrics(cs, ce, limit=1000)
    prev_pages = _page_metrics(ps, pe, limit=1000)
    if cur_pages is None:
        return []

    def bucket(pages: list[dict]) -> dict[str, float]:
        out: dict[str, float] = {}
        for p in pages:
            sec = _section_for_path(p["path"], url_sections)
            out[sec] = out.get(sec, 0.0) + p["sessions"]
        return out

    cur = bucket(cur_pages)
    prev = bucket(prev_pages or [])
    total = sum(cur.values()) or 1.0
    rows = [{"section": sec, "sessions": s, "pct": s / total * 100,
            **_delta(s, prev.get(sec, 0.0))}
           for sec, s in cur.items()]
    rows.sort(key=lambda r: -r["sessions"])
    return rows


def autopilot_contribution(session, days: int = 30) -> list[dict]:
    """Sessions the autopilot itself generated, by platform — from our own
    stored PostMetrics (no extra GA4 call: this data is already collected)."""
    from datetime import timedelta

    from sqlalchemy import func, select

    from app.models import PostMetrics, utcnow

    since = utcnow() - timedelta(days=days)
    rows = session.execute(
        select(Post.channel, func.sum(PostMetrics.ga_sessions))
        .join(PostMetrics, PostMetrics.post_id == Post.id)
        .where(Post.published_at >= since)
        .group_by(Post.channel)
    ).all()
    out = [{"channel": ch, "sessions": int(total or 0)} for ch, total in rows]
    out.sort(key=lambda r: -r["sessions"])
    return out


def dashboard(session) -> dict:
    """Everything the Portāls page needs, best-effort per section so one
    failing call doesn't blank the whole page."""
    if not configured():
        return {"configured": False}

    def _safe(build, default):
        try:
            return build()
        except Exception as e:  # noqa: BLE001
            log.warning("GA4 dashboard section failed: %s", e)
            return default

    return {
        "configured": True,
        "overview": _cached("overview", lambda: _safe(overview_windows, {})),
        "channels": _cached("channels", lambda: _safe(traffic_sources, [])),
        "top_content": _cached("top_content", lambda: _safe(top_content, [])),
        "sections": _cached("sections", lambda: _safe(section_breakdown, [])),
        "autopilot": _safe(lambda: autopilot_contribution(session), []),
    }
