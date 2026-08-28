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
                # GA4 requires filtered dimensions to be present in the request
                "dimensions": [{"name": "sessionManualAdContent"},
                               {"name": "sessionCampaignName"}],
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

    url_sections = config.url_sections()
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

    url_sections = config.url_sections()
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


# --- Explore: filterable, period-driven analytics (merged Statistika) ------
#
# Newsroom-dashboard core per industry practice (Chartbeat/Parse.ly):
# engaged time + recirculation up front, then sources, sections, authors,
# geo/devices — all under one period + section filter.

PERIODS = {
    "today": ("Šodiena", 1), "yesterday": ("Vakardiena", 1),
    "7d": ("7 dienas", 7), "30d": ("30 dienas", 30), "90d": ("90 dienas", 90),
}


def resolve_period(period: str, date_from: str = "", date_to: str = "") -> dict:
    """Normalise UI input into GA4 date strings + an equal previous window."""
    from datetime import date, timedelta

    if period == "custom" and date_from and date_to:
        try:
            f, t = date.fromisoformat(date_from), date.fromisoformat(date_to)
        except ValueError:
            return resolve_period("7d")
        if t < f:
            f, t = t, f
        span = (t - f).days + 1
        return {"key": "custom", "label": f"{f:%d.%m.} – {t:%d.%m.%Y}",
                "start": f.isoformat(), "end": t.isoformat(),
                "prev_start": (f - timedelta(days=span)).isoformat(),
                "prev_end": (f - timedelta(days=1)).isoformat(),
                "granularity": "date" if span > 1 else "hour"}
    if period == "today":
        return {"key": "today", "label": "Šodiena", "start": "today", "end": "today",
                "prev_start": "yesterday", "prev_end": "yesterday",
                "granularity": "hour"}
    if period == "yesterday":
        return {"key": "yesterday", "label": "Vakardiena",
                "start": "yesterday", "end": "yesterday",
                "prev_start": "2daysAgo", "prev_end": "2daysAgo",
                "granularity": "hour"}
    days = {"7d": 7, "30d": 30, "90d": 90}.get(period, 7)
    key = f"{days}d"
    return {"key": key, "label": PERIODS[key][0],
            "start": f"{days}daysAgo", "end": "yesterday",
            "prev_start": f"{days * 2}daysAgo", "prev_end": f"{days + 1}daysAgo",
            "granularity": "date"}


def _section_filter(section: str) -> dict | None:
    """GA4 pagePath filter for one CMS section. Nested paths are handled by
    exclusion: news = /zinas/ minus the segments of every other section."""
    from app import config

    url_sections = config.url_sections()
    include = [seg for seg, sec in url_sections.items() if sec == section]
    if not include:
        return None
    exclude = [seg for seg, sec in url_sections.items() if sec != section]

    def _contains(seg: str) -> dict:
        return {"filter": {"fieldName": "pagePath",
                           "stringFilter": {"matchType": "CONTAINS",
                                            "value": f"/{seg}/"}}}

    expr: dict = {"orGroup": {"expressions": [_contains(s) for s in include]}}
    if exclude:
        expr = {"andGroup": {"expressions": [
            expr, {"notExpression": {"orGroup": {
                "expressions": [_contains(s) for s in exclude]}}}]}}
    return expr


def _dim_report(prop: str, start: str, end: str, dimension: str,
                metrics: list[str], dim_filter: dict | None, limit: int) -> list[dict]:
    """One-dimension report. GA4 only allows filtering on requested
    dimensions, so a pagePath (section) filter adds pagePath to the request
    and the rows are re-aggregated by the asked-for dimension."""
    dims = [{"name": dimension}]
    if dim_filter:
        dims.append({"name": "pagePath"})
    body: dict = {
        "dateRanges": [{"startDate": start, "endDate": end}],
        "dimensions": dims,
        "metrics": [{"name": m} for m in metrics],
        "orderBys": [{"metric": {"metricName": metrics[0]}, "desc": True}],
        "limit": limit if not dim_filter else 100000,
    }
    if dim_filter:
        body["dimensionFilter"] = dim_filter
    data = _report(prop, body)
    if data is None:
        return []
    agg: dict[str, dict[str, float]] = {}
    order: list[str] = []
    for r in data.get("rows") or []:
        name = r["dimensionValues"][0]["value"]
        if name not in agg:
            agg[name] = {m: 0.0 for m in metrics}
            order.append(name)
        for i, m in enumerate(metrics):
            agg[name][m] += float(r["metricValues"][i]["value"] or 0)
    rows = [{"name": n, **agg[n]} for n in order]
    rows.sort(key=lambda r: -r[metrics[0]])
    return rows[:limit]


def _kpis(prop: str, p: dict, dim_filter: dict | None) -> dict:
    metrics = ["sessions", "activeUsers", "screenPageViews",
               "userEngagementDuration", "engagementRate"]

    def totals(start: str, end: str) -> dict[str, float] | None:
        if dim_filter:
            rows = _dim_report(prop, start, end, "pagePath", metrics,
                               dim_filter, limit=100000)
            if rows and rows[0].get("_error"):
                return None
            out = {m: sum(r[m] for r in rows) for m in metrics}
            # engagementRate is a ratio — recompute from engaged duration proxy
            n = len(rows) or 1
            out["engagementRate"] = sum(r["engagementRate"] for r in rows) / n
            return out
        body: dict = {"dateRanges": [{"startDate": start, "endDate": end}],
                      "metrics": [{"name": m} for m in metrics]}
        data = _report(prop, body)
        if data is None:
            return None
        rows = data.get("rows") or []
        if not rows:
            return {m: 0.0 for m in metrics}
        vals = rows[0]["metricValues"]
        return {m: float(vals[i]["value"] or 0) for i, m in enumerate(metrics)}

    cur = totals(p["start"], p["end"])
    prev = totals(p["prev_start"], p["prev_end"])
    if cur is None:
        return {}
    prev = prev or {m: 0.0 for m in metrics}

    def kpi(name, cur_v, prev_v):
        return {"name": name, **_delta(cur_v, prev_v)}

    out = {
        "sessions": kpi("Sesijas", cur["sessions"], prev["sessions"]),
        "users": kpi("Aktīvie lietotāji", cur["activeUsers"], prev["activeUsers"]),
        "pageviews": kpi("Lapu skatījumi", cur["screenPageViews"],
                         prev["screenPageViews"]),
        # Chartbeat-style core: engaged time per session + recirculation proxy
        "engaged": kpi("Iesaistes laiks / sesija",
                       cur["userEngagementDuration"] / cur["sessions"]
                       if cur["sessions"] else 0,
                       prev["userEngagementDuration"] / prev["sessions"]
                       if prev["sessions"] else 0),
        "recirculation": kpi("Lapas / sesija (recirkulācija)",
                             cur["screenPageViews"] / cur["sessions"]
                             if cur["sessions"] else 0,
                             prev["screenPageViews"] / prev["sessions"]
                             if prev["sessions"] else 0),
        "engagement_rate": kpi("Iesaistes līmenis",
                               cur["engagementRate"] * 100,
                               prev["engagementRate"] * 100),
    }
    return out


def _timeseries(prop: str, p: dict, dim_filter: dict | None) -> list[dict]:
    dim = "dateHour" if p["granularity"] == "hour" else "date"
    rows = _dim_report(prop, p["start"], p["end"], dim, ["sessions"],
                       dim_filter, limit=2000)
    rows.sort(key=lambda r: r["name"])
    out = []
    for r in rows:
        raw = r["name"]
        label = (f"{raw[8:10]}:00" if dim == "dateHour" and len(raw) >= 10
                 else f"{raw[6:8]}.{raw[4:6]}." if len(raw) == 8 else raw)
        out.append({"label": label, "value": r["sessions"]})
    return out


def _authors(prop: str, p: dict, dim_filter: dict | None,
             limit: int = 12) -> tuple[list[dict] | None, str]:
    """(rows, error) — author breakdown via a GA4 custom dimension. Runs
    ONLY when the dimension name is configured in Konti; its failure never
    leaks into the page-level error banner."""
    global _last_error
    from app import credentials

    dim = credentials.get("ga4_author_dimension")
    if not dim:
        return None, ""
    prev_err = _last_error
    rows = _dim_report(prop, p["start"], p["end"], dim,
                       ["screenPageViews", "sessions", "userEngagementDuration"],
                       dim_filter, limit)
    author_err = _last_error if not rows else ""
    _last_error = prev_err
    out = [{"author": r["name"], "pageviews": r["screenPageViews"],
            "sessions": r["sessions"],
            "avg_engagement_s": (r["userEngagementDuration"] / r["screenPageViews"]
                                 if r["screenPageViews"] else 0)}
           for r in rows if r["name"] not in ("", "(not set)")]
    return (out or None), author_err


def explore(session, period: str, section: str = "",
            date_from: str = "", date_to: str = "") -> dict:
    """Everything the merged Statistika page needs for one period+filter."""
    if not configured():
        return {"configured": False}
    prop = property_id()
    p = resolve_period(period, date_from, date_to)
    dim_filter = _section_filter(section) if section else None
    key = f"explore:{p['start']}:{p['end']}:{section}"

    def build() -> dict:
        pages = _page_metrics_filtered(prop, p, dim_filter, limit=200)
        from app import config

        url_sections = config.url_sections()
        for pg in pages:
            pg["section"] = _section_for_path(pg["path"], url_sections)
        return {
            "configured": True,
            "period": p,
            "section": section,
            "kpis": _kpis(prop, p, dim_filter),
            "timeseries": _timeseries(prop, p, dim_filter),
            "channels": _channels_delta(prop, p, dim_filter),
            "sections": ([] if section else _sections_from_pages(
                pages, _page_metrics_filtered(
                    prop, {**p, "start": p["prev_start"], "end": p["prev_end"]},
                    None, limit=200), url_sections)),
            "top_content": pages[:15],
            "authors_result": _authors(prop, p, dim_filter),
            "countries": _dim_report(prop, p["start"], p["end"], "country",
                                     ["sessions"], dim_filter, 8),
            "cities": _dim_report(prop, p["start"], p["end"], "city",
                                  ["sessions"], dim_filter, 8),
            "devices": _dim_report(prop, p["start"], p["end"], "deviceCategory",
                                   ["sessions"], dim_filter, 4),
            "browsers": _dim_report(prop, p["start"], p["end"], "browser",
                                    ["sessions"], dim_filter, 6),
            "error": last_error(),
        }

    out = _cached(key, build)
    if "authors_result" in out:
        out["authors"], out["author_error"] = out.pop("authors_result")
    return out


def _sections_from_pages(cur_pages: list[dict], prev_pages: list[dict],
                         url_sections: dict) -> list[dict]:
    def bucket(pages: list[dict]) -> dict[str, float]:
        out: dict[str, float] = {}
        for pg in pages:
            sec = pg.get("section") or _section_for_path(pg["path"], url_sections)
            out[sec] = out.get(sec, 0.0) + pg["sessions"]
        return out

    cur, prev = bucket(cur_pages), bucket(prev_pages)
    total = sum(cur.values()) or 1.0
    rows = [{"section": sec, "sessions": v, "pct": v / total * 100,
             **_delta(v, prev.get(sec, 0.0))} for sec, v in cur.items()]
    rows.sort(key=lambda r: -r["sessions"])
    return rows


def _channels_delta(prop: str, p: dict, dim_filter: dict | None) -> list[dict]:
    def by_channel(start: str, end: str) -> dict[str, float]:
        rows = _dim_report(prop, start, end, "sessionDefaultChannelGroup",
                           ["sessions"], dim_filter, 10)
        return {r["name"]: r["sessions"] for r in rows}

    cur = by_channel(p["start"], p["end"])
    prev = by_channel(p["prev_start"], p["prev_end"])
    total = sum(cur.values()) or 1.0
    rows = [{"channel": ch, "sessions": s, "pct": s / total * 100,
             **_delta(s, prev.get(ch, 0.0))} for ch, s in cur.items()]
    rows.sort(key=lambda r: -r["sessions"])
    return rows


def _page_metrics_filtered(prop: str, p: dict, dim_filter: dict | None,
                           limit: int) -> list[dict]:
    body: dict = {
        "dateRanges": [{"startDate": p["start"], "endDate": p["end"]}],
        "dimensions": [{"name": "pagePath"}, {"name": "pageTitle"}],
        "metrics": [{"name": "screenPageViews"}, {"name": "sessions"},
                    {"name": "userEngagementDuration"}],
        "orderBys": [{"metric": {"metricName": "screenPageViews"}, "desc": True}],
        "limit": limit,
    }
    if dim_filter:
        body["dimensionFilter"] = dim_filter
    data = _report(prop, body)
    out = []
    for r in (data or {}).get("rows") or []:
        views = float(r["metricValues"][0]["value"] or 0)
        out.append({"path": r["dimensionValues"][0]["value"],
                    "title": r["dimensionValues"][1]["value"],
                    "pageviews": views,
                    "sessions": float(r["metricValues"][1]["value"] or 0),
                    "avg_engagement_s": (float(r["metricValues"][2]["value"] or 0)
                                         / views if views else 0)})
    return out


def sparkline(series: list[dict], width: int = 860, height: int = 120) -> dict:
    """Precomputed SVG geometry for the sessions trend (server-rendered)."""
    if not series:
        return {}
    values = [s["value"] for s in series]
    peak = max(values) or 1.0
    n = len(values)
    pad = 4
    step = (width - 2 * pad) / max(n - 1, 1)

    def xy(i: int, v: float) -> tuple[float, float]:
        return (pad + i * step, pad + (height - 2 * pad) * (1 - v / peak))

    pts = [xy(i, v) for i, v in enumerate(values)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = (f"{pts[0][0]:.1f},{height - pad} " + line
            + f" {pts[-1][0]:.1f},{height - pad}")
    peak_i = values.index(max(values))
    ticks = [series[0]["label"], series[n // 2]["label"], series[-1]["label"]] \
        if n >= 3 else [s["label"] for s in series]
    return {"line": line, "area": area, "width": width, "height": height,
            "last": {"x": pts[-1][0], "y": pts[-1][1], "value": values[-1],
                     "label": series[-1]["label"]},
            "peak": {"x": pts[peak_i][0], "y": pts[peak_i][1],
                     "value": values[peak_i], "label": series[peak_i]["label"]},
            "points": [{"x": x, "y": y, "label": s["label"], "value": s["value"]}
                       for (x, y), s in zip(pts, series)],
            "ticks": ticks}


# --- Raksta detaļu skats ---------------------------------------------------

def page_insight(path: str, period: str, date_from: str = "",
                 date_to: str = "") -> dict:
    """One article drill-down: KPIs, where its readers came from (channels +
    referrers), what sessions landing on it read next, and in-article video
    events (GA4 video_start/_progress/_complete when the player emits them)."""
    if not configured():
        return {"configured": False}
    prop = property_id()
    p = resolve_period(period, date_from, date_to)
    exact = {"filter": {"fieldName": "pagePath",
                        "stringFilter": {"matchType": "EXACT", "value": path}}}
    landing = {"filter": {"fieldName": "landingPagePlusQueryString",
                          "stringFilter": {"matchType": "BEGINS_WITH",
                                           "value": path}}}
    key = f"page:{p['start']}:{p['end']}:{path}"

    def build() -> dict:
        # totals: pagePath is both requested and filtered
        totals_rows = _report(prop, {
            "dateRanges": [{"startDate": p["start"], "endDate": p["end"]}],
            "dimensions": [{"name": "pagePath"}, {"name": "pageTitle"}],
            "metrics": [{"name": "screenPageViews"}, {"name": "sessions"},
                        {"name": "userEngagementDuration"}],
            "dimensionFilter": exact, "limit": 5}) or {}
        rows = totals_rows.get("rows") or []
        views = sum(float(r["metricValues"][0]["value"] or 0) for r in rows)
        sessions_n = sum(float(r["metricValues"][1]["value"] or 0) for r in rows)
        engagement = sum(float(r["metricValues"][2]["value"] or 0) for r in rows)
        title = rows[0]["dimensionValues"][1]["value"] if rows else path

        def two_dim(dim: str, flt: dict, limit: int = 12) -> list[dict]:
            data = _report(prop, {
                "dateRanges": [{"startDate": p["start"], "endDate": p["end"]}],
                "dimensions": [{"name": dim},
                               {"name": ("pagePath" if flt is exact
                                         else "landingPagePlusQueryString")}],
                "metrics": [{"name": "sessions"}],
                "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
                "dimensionFilter": flt, "limit": 5000}) or {}
            agg: dict[str, float] = {}
            for r in data.get("rows") or []:
                name = r["dimensionValues"][0]["value"]
                agg[name] = agg.get(name, 0.0) + float(r["metricValues"][0]["value"] or 0)
            out = [{"name": n, "sessions": v} for n, v in agg.items()]
            out.sort(key=lambda x: -x["sessions"])
            return out[:limit]

        next_reads = [r for r in two_dim("pagePath", landing, 14)
                      if r["name"].rstrip("/") != path.rstrip("/")][:10]
        events = two_dim("eventName", exact, 30)
        video = [e for e in events
                 if e["name"] in ("video_start", "video_progress", "video_complete")]
        return {
            "configured": True, "period": p, "path": path, "title": title,
            "pageviews": views, "sessions": sessions_n,
            "avg_engagement_s": engagement / views if views else 0,
            "channels": two_dim("sessionDefaultChannelGroup", exact, 10),
            "referrers": [r for r in two_dim("pageReferrer", exact, 14)
                          if r["name"] not in ("", "(not set)")][:10],
            "next_reads": next_reads,
            "video_events": video,
            "error": last_error(),
        }

    return _cached(key, build)
