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
from app.formats import choose_format
from app.models import Article, Evaluation, Post, get_setting, utcnow
from app.rules_engine import evaluate_all
from app.slots import find_slot
from app import runtime

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
        maybe_correct_section(article, decision)

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

            fmt, card_media = resolve_format(session, channel, cfg, article, ch_dec)

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
            if fmt in ("card_carousel", "reel"):
                media = card_media
            elif fmt == "photo" and images:
                media = [branded_photo(article, photo_base_image(article, idx),
                                       cfg.get("platform", ""))]
            elif fmt == "story":
                media = story_media(article, images[idx] if idx < len(images)
                                    else (images[0] if images else ""))
                if not media:
                    session.add(Evaluation(article_id=article.id, channel=channel,
                                           outcome="blocked",
                                           reason="story needs an image / renderer"))
                    continue
            elif fmt == "photo_album":
                media = images[:10]
            else:
                media = []
            post = Post(
                article_id=article.id, channel=channel, format=fmt,
                copy=copy, hashtags=hashtags, media=media,
                link_url=article.canonical_url or article.url,
                scheduled_at=slot, state="scheduled", dry_run=runtime.is_dry_run(session),
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


def maybe_correct_section(article, decision: dict) -> None:
    """Feed hints mislabel sections (a 'must' feed tagging NATO news as
    entertainment); the AI classifies from content. A section derived from
    the term-ID mapping is authoritative and never overridden."""
    sec = decision.get("section") or ""
    if (sec in ("news", "sport", "entertainment") and sec != article.section
            and (article.raw_json or {}).get("_section_src") != "terms"):
        log.info("section corrected for article %s: %s -> %s",
                 article.id, article.section, sec)
        article.section = sec


# Best-practice photo sizes: FB feed shows 4:5 uncropped and it takes the
# most screen space; X/Threads are safest at 1:1.
PHOTO_SIZES = {"facebook_page": (1080, 1350), "instagram": (1080, 1350)}


def photo_base_image(article, idx: int = 0) -> str:
    """Base image for branded renders. When the chosen image is portrait —
    on tv3.lv usually a 'photopost' graphic with its own baked-in headline —
    and the feed also carries a horizontal photo, use the horizontal one:
    the title plate then sits on a clean photo instead of doubling text.
    Toggle: rules.yaml photo_prefer_landscape."""
    images = article.images or []
    if not images:
        return ""
    chosen = images[min(idx, len(images) - 1)]
    if not config.load_rules().get("photo_prefer_landscape", True):
        return chosen
    from app import imageinfo

    if imageinfo.is_portrait(article, chosen):
        alt = imageinfo.landscape_image(article)
        if alt:
            return alt
    return chosen


def branded_photo(article, image_url: str, platform: str = "") -> str:
    """Photo posts carry the article image with the tv3.lv title plate
    burned in (rules.yaml photo_title_overlay). Falls back to the raw
    image when the renderer is unavailable or fails."""
    from app import cards

    rules = config.load_rules()
    if not rules.get("photo_title_overlay", True) or not cards.renderer_available():
        return image_url
    width, height = PHOTO_SIZES.get(platform, (1080, 1080))
    try:
        return cards.render_share_image(article.title, article.section, image_url,
                                        width=width, height=height)
    except Exception as e:  # noqa: BLE001
        log.warning("share image render failed for article %s: %s", article.id, e)
        return image_url


def story_media(article, image_url: str) -> list[str]:
    """Vertical branded story image; falls back to the raw article image;
    empty when there is nothing visual to post."""
    from app import cards

    if cards.renderer_available():
        try:
            return [cards.render_story(article.title, article.section, image_url)]
        except Exception as e:  # noqa: BLE001
            log.warning("story render failed for article %s: %s", article.id, e)
    return [image_url] if image_url else []


def resolve_format(session, channel: str, cfg: dict, article, ch_dec: dict):
    """Format for this post. A carousel happens only when the AI proposed it
    AND provided usable card points AND the renderer works; otherwise the
    diversity-aware chooser decides and media is derived from the article."""
    from app import cards

    ai_fmt = ch_dec.get("format")
    if ai_fmt == "card_carousel" and "card_carousel" in (cfg.get("formats") or []):
        points = [p.strip() for p in (ch_dec.get("card_points") or [])
                  if isinstance(p, str) and p.strip()][:5]
        if len(points) >= 3 and cards.renderer_available():
            tag = "#" + (article.labels[0].upper().replace(" ", "")
                         if article.labels else article.section.upper())
            image = photo_base_image(article)
            question = (ch_dec.get("card_end_question")
                        or "Uzzini visu stāstu tv3.lv").strip()
            try:
                media = cards.render_cards(article.title, article.section, tag,
                                           points, image, question)
                return "card_carousel", media
            except Exception as e:  # noqa: BLE001 — never lose the post over a render
                log.warning("card render failed for article %s: %s", article.id, e)
        ai_fmt = None  # fall back to a normal format
    if ai_fmt == "reel" and "reel" in (cfg.get("formats") or []):
        from app import reels

        # Real article clip beats a slideshow every time; the tv3.lv/video
        # 9:16 clips come through the feed as a video URL on the item.
        video = reels.article_video(article)
        if video and reels.available():
            try:
                return "reel", [reels.build_video_reel(video)]
            except Exception as e:  # noqa: BLE001
                log.warning("video reel failed for article %s: %s", article.id, e)
        points = [p.strip() for p in (ch_dec.get("card_points") or [])
                  if isinstance(p, str) and p.strip()][:3]
        image = photo_base_image(article)
        if len(points) >= 2 and image and reels.available():
            try:
                media = reels.build_reel(article.title, article.section,
                                         image, points)
                return "reel", [media]
            except Exception as e:  # noqa: BLE001 — never lose the post over a render
                log.warning("reel build failed for article %s: %s", article.id, e)
        ai_fmt = None
    fmt = choose_format(session, channel, cfg, article, ai_fmt)
    # A portrait og-image gets butchered by Facebook's 1.91:1 link-card
    # crop (baked-in title plate cut off). Switch to photo: we render our
    # own correctly sized branded image there.
    if (fmt == "link" and (article.images or [])
            and "photo" in (cfg.get("formats") or [])
            and config.load_rules().get("portrait_link_to_photo", True)):
        from app import imageinfo

        if imageinfo.orientation(article) == "portrait":
            fmt = "photo"
    return fmt, []


def refresh_missing_media(session, post, platform: str) -> None:
    """Re-render photo/story media just before publishing when needed:
    the rendered file was wiped (deploy without the volume), or the stored
    media is the raw article URL because rendering failed at decision time —
    a later-recovered renderer then still gets the branded version out."""
    from pathlib import Path

    if post.article is None or post.format not in ("photo", "story"):
        # card_carousel and reel can't be regenerated here (the AI's card
        # points aren't stored on the post) — the adapter fails with a clear
        # message and the editor can cancel or let the article be re-decided
        return
    article = post.article
    media = post.media or []
    current = str(media[0]) if media else ""
    local_gone = bool(current) and not current.startswith("http") and not Path(current).exists()
    raw_fallback = current.startswith("http")
    if not (local_gone or raw_fallback or not media):
        return
    if post.format == "photo":
        image = photo_base_image(article)
        new = [branded_photo(article, image, platform)] if image else []
    else:
        image = (article.images or [""])[0]
        new = story_media(article, image)
    if new and new != media:
        post.media = new
        session.commit()


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
        refresh_missing_media(session, post, platform)
        post.state = "publishing"
        post.attempts += 1
        session.commit()
        try:
            link = add_utm(post.link_url, platform, post.id) if post.link_url else ""
            # SocialFlow-style tactic: on FB/IG image posts the link goes into
            # the first comment, keeping the caption clean for reach (on IG
            # caption links aren't clickable at all)
            first_comment_link = bool(
                link and platform in ("facebook_page", "instagram")
                and post.format in ("photo", "photo_album", "card_carousel", "reel")
                and config.load_rules().get("link_in_first_comment", True))
            text = assemble_post_text(post.copy, post.hashtags or [],
                                      "" if first_comment_link else link, platform)
            adapter = get_adapter(platform)
            post.platform_post_id = adapter.publish(
                text=text, link=link, images=post.media or [], fmt=post.format)
            post.state = "published"
            post.published_at = utcnow()
            post.error = ""
            published += 1
            if first_comment_link and post.platform_post_id:
                try:
                    adapter.comment(post.platform_post_id, link)
                except Exception as e:  # noqa: BLE001 — post stands even if it fails
                    log.warning("first-comment link failed for post %s: %s", post.id, e)
                    post.error = f"comment failed: {e}"
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

    from app import ga4

    collected += ga4.collect(session)
    return collected


def weekly_report(session) -> str:
    """Human summary of the last 30 days, sent to Slack/log weekly."""
    from app import priors

    lines = ["TV3 Autopilot — nedēļas kopsavilkums (pēdējās 30 dienas):"]
    for channel in config.load_channels():
        s = priors.channel_summary(session, channel)
        if not s["posts"]:
            continue
        fmt = ", ".join(f"{f['format']}: {f['avg']:.0f} (n={f['n']})"
                        for f in s["formats"][:4])
        hours = (", stiprākās stundas " + ", ".join(f"{h}:00" for h in s["best_hours"])
                 if s["best_hours"] else "")
        lines.append(f"• {channel}: {s['posts']} ieraksti, {s['sessions']} GA sesijas, "
                     f"{s['clicks']} klikšķi. Formāti: {fmt}{hours}")
    top = priors.top_posts(session, 5)
    if top:
        lines.append("Top ieraksti:")
        for r in top:
            title = r["post"].article.title[:70] if r["post"].article else ""
            lines.append(f"  {r['score']:.0f} — [{r['channel']}/{r['format']}] {title}")
    report = "\n".join(lines)
    alert(report) if len(lines) > 1 else None
    return report
