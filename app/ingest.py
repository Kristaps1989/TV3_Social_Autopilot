"""Feed ingestor: polls the WordPress social feeds and normalises items.

The exact feed format is not yet confirmed (see docs/feed-format.md), so the
parser accepts both JSON (list of objects, or {items:[...]}) and RSS/Atom.
Field lookup is defensive: several candidate key names per field.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx
from sqlalchemy import select

from app import config
from app.models import Article, Post, utcnow

log = logging.getLogger(__name__)

STATUSES = ("now", "must", "can", "dont")


def _first(item: dict, *keys: str, default: Any = "") -> Any:
    for k in keys:
        v = item.get(k)
        if v not in (None, ""):
            return v
    return default


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).replace(tzinfo=None)
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(str(value)[:25], fmt)
            if dt.tzinfo:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except ValueError:
            continue
    try:
        parsed = feedparser._parse_date(str(value))  # type: ignore[attr-defined]
        if parsed:
            return datetime(*parsed[:6])
    except Exception:
        pass
    return None


def derive_section(term_ids: list, hint: str, term_sections: dict) -> str:
    for tid in term_ids:
        try:
            key = int(tid)
        except (TypeError, ValueError):
            continue
        if key in term_sections:
            return term_sections[key]
        if str(key) in term_sections:
            return term_sections[str(key)]
    return hint or "news"


def normalize_json_item(item: dict, feed_name: str, hint: str, term_sections: dict) -> dict:
    url = _first(item, "url", "link", "permalink", "canonical_url")
    images = item.get("images") or []
    if not images:
        for key in ("image", "featured_image", "thumbnail", "og_image"):
            if item.get(key):
                images = [item[key]]
                break
    term_ids = item.get("term_ids") or item.get("terms") or item.get("categories_ids") or []
    if isinstance(term_ids, str):
        term_ids = [t for t in term_ids.split(",") if t.strip()]
    status = str(_first(item, "status", "social_status", "editor_status", default="can")).lower()
    if status not in STATUSES:
        status = "can"
    return {
        "guid": str(_first(item, "guid", "id", "post_id", default=url)),
        "url": url,
        "canonical_url": _first(item, "canonical_url", "canonical", default=url),
        "title": str(_first(item, "title", "headline")).strip(),
        "lead": str(_first(item, "lead", "excerpt", "description", "summary")).strip(),
        "categories": item.get("categories") or [],
        "term_ids": [str(t) for t in term_ids],
        "images": images,
        "published_at": _parse_dt(_first(item, "published_at", "date", "pubDate", "published")),
        "editor_status": status,
        "editor_timeframe": str(_first(item, "timeframe", "social_timeframe")).lower(),
        "section": derive_section(term_ids, hint, term_sections),
        "feed_name": feed_name,
        "raw_json": item,
    }


def normalize_rss_entry(entry: Any, feed_name: str, hint: str, term_sections: dict) -> dict:
    url = getattr(entry, "link", "")
    images = []
    for m in getattr(entry, "media_content", []) or []:
        if m.get("url"):
            images.append(m["url"])
    for enc in getattr(entry, "enclosures", []) or []:
        if enc.get("href") and "image" in enc.get("type", ""):
            images.append(enc["href"])
    tags = [t.get("term", "") for t in getattr(entry, "tags", []) or []]
    published = None
    if getattr(entry, "published_parsed", None):
        published = datetime(*entry.published_parsed[:6])
    return {
        "guid": getattr(entry, "id", "") or url,
        "url": url,
        "canonical_url": url,
        "title": getattr(entry, "title", "").strip(),
        "lead": getattr(entry, "summary", "").strip(),
        "categories": tags,
        "term_ids": [],
        "images": images,
        "published_at": published,
        "editor_status": "can",  # RSS carries no status; the feed URL already filters it
        "editor_timeframe": "",
        "section": hint or "news",
        "feed_name": feed_name,
        "raw_json": {},
    }


def parse_feed_body(body: str, content_type: str, feed_name: str, hint: str,
                    term_sections: dict) -> list[dict]:
    body = body.strip()
    if body.startswith(("[", "{")) or "json" in content_type:
        import json

        data = json.loads(body)
        items = data if isinstance(data, list) else (
            data.get("items") or data.get("posts") or data.get("articles") or []
        )
        return [normalize_json_item(i, feed_name, hint, term_sections) for i in items
                if isinstance(i, dict)]
    parsed = feedparser.parse(body)
    return [normalize_rss_entry(e, feed_name, hint, term_sections) for e in parsed.entries]


def status_from_feed_url(url: str) -> str:
    """The status segment of the feed URL applies to every item in it."""
    parts = [p for p in url.rstrip("/").split("/") if p]
    for p in parts:
        if p in STATUSES:
            return p
    return ""


def upsert_article(session, data: dict) -> tuple[Article, bool, bool]:
    """Insert or update. Returns (article, created, status_changed)."""
    existing = session.execute(
        select(Article).where(Article.guid == data["guid"])
    ).scalar_one_or_none()
    if existing is None:
        article = Article(**data)
        session.add(article)
        session.flush()
        return article, True, False

    status_changed = existing.editor_status != data["editor_status"]
    existing.editor_status = data["editor_status"]
    existing.editor_timeframe = data["editor_timeframe"]
    existing.title = data["title"] or existing.title
    existing.lead = data["lead"] or existing.lead
    existing.images = data["images"] or existing.images
    existing.last_seen_at = utcnow()
    if status_changed:
        # A status change means the editor changed their mind: re-decide.
        existing.decided_at = None
    return existing, False, status_changed


def cancel_posts_for_dont(session, article: Article) -> int:
    """Editor flipped to 'dont' — cancel anything not yet published."""
    cancelled = 0
    for post in article.posts:
        if post.state in ("proposed", "scheduled"):
            post.state = "cancelled"
            post.error = "editor set status to dont"
            cancelled += 1
    return cancelled


def run_ingest(session) -> dict:
    """Poll all active feeds. Returns a summary dict."""
    feeds = config.load_feeds()
    term_sections = feeds.pop("term_sections", {}) or {}
    summary = {"fetched": 0, "created": 0, "updated": 0, "cancelled": 0, "errors": []}

    for name, cfg in feeds.items():
        if not isinstance(cfg, dict) or not cfg.get("active", True):
            continue
        url = cfg.get("url", "")
        hint = cfg.get("section_hint", "")
        try:
            resp = httpx.get(url, timeout=20, follow_redirects=True,
                             headers={"User-Agent": "TV3-Social-Autopilot/1.0"})
            resp.raise_for_status()
            items = parse_feed_body(resp.text, resp.headers.get("content-type", ""),
                                    name, hint, term_sections)
        except Exception as e:  # noqa: BLE001 — one bad feed must not stop the rest
            log.warning("feed %s failed: %s", name, e)
            summary["errors"].append(f"{name}: {e}")
            continue

        url_status = status_from_feed_url(url)
        for data in items:
            if not data["url"] or not data["title"]:
                continue
            if url_status and data["editor_status"] == "can" and "status" not in data["raw_json"]:
                # No explicit per-item status: inherit the feed URL's status segment.
                data["editor_status"] = url_status
            article, created, status_changed = upsert_article(session, data)
            summary["fetched"] += 1
            summary["created"] += int(created)
            summary["updated"] += int(not created)
            if article.editor_status == "dont":
                summary["cancelled"] += cancel_posts_for_dont(session, article)

    session.commit()
    return summary
