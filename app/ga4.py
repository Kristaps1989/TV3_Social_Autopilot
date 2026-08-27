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
