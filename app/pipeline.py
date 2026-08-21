"""Pipeline steps wired together by the scheduler:

  ingest -> evaluate (rules) + decide (AI) -> create posts -> publish due
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from adapters import get_adapter
from adapters.base import PublishError
from app import config
from app.best_practices import add_utm, assemble_post_text, sanitize_copy
from app.decide import decide
from app.models import Article, Evaluation, Post, get_setting, utcnow
from app.rules_engine import evaluate_all
from app.slots import find_slot

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


def paused(session, channel: str | None = None) -> bool:
    if get_setting(session, "kill_switch") == "on":
        return True
    if channel and get_setting(session, f"pause:{channel}") == "on":
        return True
    return False


def run_decisions(session, limit: int = 20) -> int:
    """Evaluate + decide undecided articles, create scheduled posts."""
    now = utcnow()
    articles = session.execute(
        select(Article)
        .where(Article.decided_at.is_(None), Article.editor_status != "dont")
        .order_by(Article.first_seen_at)
        .limit(limit)
    ).scalars().all()

    channels_cfg = config.load_channels()
    created = 0

    for article in articles:
        verdicts = evaluate_all(article, now)
        for channel, verdict in verdicts.items():
            session.add(Evaluation(article_id=article.id, channel=channel,
                                   outcome=verdict.outcome, reason=verdict.reason))
        if all(v.outcome == "blocked" for v in verdicts.values()):
            article.decided_at = now
            session.commit()
            continue

        decision = decide(article, verdicts, session)

        if not decision.get("publish"):
            for channel, verdict in verdicts.items():
                if verdict.outcome != "blocked":
                    session.add(Evaluation(article_id=article.id, channel=channel,
                                           outcome="ai_skip",
                                           reason=decision.get("reason", "")))
            session.commit()
            continue

        for ch_dec in decision.get("channels") or []:
            channel = ch_dec.get("channel", "")
            verdict = verdicts.get(channel)
            cfg = channels_cfg.get(channel)
            if verdict is None or cfg is None or verdict.outcome == "blocked":
                continue

            # duplicate guard: same article + channel only once
            dup = session.execute(
                select(Post).where(Post.article_id == article.id, Post.channel == channel,
                                   Post.state.in_(("proposed", "scheduled", "publishing",
                                                   "published")))
            ).scalar_one_or_none()
            if dup:
                continue

            fmt = ch_dec.get("format") or "link"
            if fmt not in (cfg.get("formats") or [fmt]):
                fmt = (cfg.get("formats") or ["link"])[0]

            platform = cfg.get("platform", "")
            copy, hashtags, fixes = sanitize_copy(
                ch_dec.get("copy") or article.title,
                ch_dec.get("hashtags") or [],
                platform, article.sensitivity, reserve_link_chars=True,
            )

            preferred = None
            if isinstance(ch_dec.get("preferred_hour"), int):
                ph = ch_dec["preferred_hour"]
                candidate = now.replace(minute=0, second=0, microsecond=0)
                for _ in range(30):
                    from zoneinfo import ZoneInfo
                    local = candidate.replace(tzinfo=ZoneInfo("UTC")).astimezone(
                        ZoneInfo(config.TIMEZONE))
                    if local.hour == ph and candidate >= now:
                        preferred = candidate
                        break
                    candidate += timedelta(hours=1)

            slot = find_slot(session, channel, cfg, verdict,
                             article.section, fmt, article.title, now, preferred)
            if slot is None:
                session.add(Evaluation(article_id=article.id, channel=channel,
                                       outcome="blocked",
                                       reason="no valid slot (cadence/diversity/similarity)"))
                continue

            idx = ch_dec.get("image_index") or 0
            images = article.images or []
            media = ([images[idx]] if fmt == "photo" and idx < len(images)
                     else images[:10] if fmt == "photo_album" else [])
            post = Post(
                article_id=article.id, channel=channel, format=fmt,
                copy=copy, hashtags=hashtags, media=media,
                link_url=article.canonical_url or article.url,
                scheduled_at=slot, state="scheduled", dry_run=config.DRY_RUN,
            )
            session.add(post)
            session.flush()
            session.add(Evaluation(article_id=article.id, channel=channel,
                                   outcome="posted",
                                   reason=f"scheduled {slot:%Y-%m-%d %H:%M} UTC as {fmt}"
                                          + (f" (fixes: {', '.join(fixes)})" if fixes else "")))
            created += 1
        session.commit()
    return created


def publish_due(session) -> int:
    """Publish posts whose time has come. Cancels posts whose article turned 'dont'."""
    now = utcnow()
    due = session.execute(
        select(Post).where(Post.state == "scheduled", Post.scheduled_at <= now)
        .order_by(Post.scheduled_at)
    ).scalars().all()

    published = 0
    channels_cfg = config.load_channels()
    for post in due:
        if post.article and post.article.editor_status == "dont":
            post.state = "cancelled"
            post.error = "editor set status to dont"
            session.commit()
            continue
        if paused(session, post.channel):
            continue  # stays scheduled; resumes when unpaused

        platform = (channels_cfg.get(post.channel) or {}).get("platform", "")
        post.state = "publishing"
        post.attempts += 1
        session.commit()
        try:
            link = add_utm(post.link_url, platform, post.id) if post.link_url else ""
            text = assemble_post_text(post.copy, post.hashtags or [], link, platform)
            adapter = get_adapter(platform)
            post.platform_post_id = adapter.publish(
                text=text, link=link, images=post.media or [], fmt=post.format)
            post.state = "published"
            post.published_at = utcnow()
            post.error = ""
            published += 1
        except PublishError as e:
            if e.retryable and post.attempts < MAX_ATTEMPTS:
                post.state = "scheduled"
                post.scheduled_at = utcnow() + timedelta(minutes=5 * post.attempts)
                post.error = f"retry {post.attempts}: {e}"
            else:
                post.state = "failed"
                post.error = str(e)
                alert(f"Post {post.id} -> {post.channel} failed: {e}")
        except Exception as e:  # noqa: BLE001
            post.state = "failed"
            post.error = f"unexpected: {e}"
            alert(f"Post {post.id} -> {post.channel} crashed: {e}")
        session.commit()
    return published


def alert(message: str) -> None:
    log.error("ALERT: %s", message)
    if config.SLACK_WEBHOOK_URL:
        try:
            import httpx

            httpx.post(config.SLACK_WEBHOOK_URL,
                       json={"text": f"TV3 Autopilot: {message}"}, timeout=10)
        except Exception:  # noqa: BLE001
            log.exception("slack alert failed")


def collect_metrics(session) -> int:
    """Pull platform insights for recently published posts (1h..72h old)."""
    now = utcnow()
    rows = session.execute(
        select(Post).where(Post.state == "published",
                           Post.published_at >= now - timedelta(hours=72),
                           Post.dry_run.is_(False))
    ).scalars().all()
    channels_cfg = config.load_channels()
    collected = 0
    for post in rows:
        platform = (channels_cfg.get(post.channel) or {}).get("platform", "")
        adapter = get_adapter(platform)
        data = adapter.fetch_insights(post.platform_post_id)
        if data:
            from app.models import PostMetrics

            session.add(PostMetrics(post_id=post.id, **data))
            collected += 1
    session.commit()
    return collected
