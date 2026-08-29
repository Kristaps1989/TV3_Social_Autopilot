"""Pipeline steps wired together by the scheduler:

  ingest -> evaluate (rules) + decide (AI) -> create posts -> publish due
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from adapters import get_adapter
from adapters.base import PublishError
from app import config, shortlinks
from app.best_practices import add_utm, assemble_post_text, sanitize_copy
from app.decide import decide
from app.formats import choose_format, mix_deficit, recent_format_shares
from app.models import Article, Evaluation, Post, get_setting, utcnow
from app.rules_engine import evaluate_all
from app.slots import plan_slot
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
        if retry_pending(article, now):
            continue  # queue was full: waiting out the backoff before retrying
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
        scheduled_here = 0

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

            # One post per article and channel — except the deliberate second
            # wave: a strong story may go out twice in DIFFERENT formats (the
            # photo carries the visual, the link post an hour later carries the
            # clickable card that paid campaigns can amplify).
            existing = session.execute(
                select(Post).where(Post.article_id == article.id, Post.channel == channel,
                                   Post.state.in_(("proposed", "scheduled", "publishing",
                                                   "published")))
            ).scalars().all()
            repost_at = repost_offset(article, cfg, existing)
            if existing and repost_at is None:
                continue

            fmt, card_media = resolve_format(session, channel, cfg, article, ch_dec)
            if any(p.format == fmt for p in existing):
                # the second wave only earns its place as a different format
                session.add(Evaluation(article_id=article.id, channel=channel,
                                       outcome="blocked",
                                       reason=f"atkārtojums tajā pašā formātā ({fmt})"))
                continue

            platform = cfg.get("platform", "")
            copy, hashtags, fixes = sanitize_copy(
                ch_dec.get("copy") or article.title,
                ch_dec.get("hashtags") or [],
                platform, article.sensitivity, reserve_link_chars=True,
            )

            preferred = repost_at
            # asap režīmā AI ieteiktā stunda NEDRĪKST aizkavēt saturu (tā
            # pārcēla postus uz nākamās dienas pusdienlaiku); to izmanto
            # tikai optimize režīms
            asap_mode = str(config.load_rules().get(
                "scheduling_mode", "asap")).lower() != "optimize"
            if not asap_mode and isinstance(ch_dec.get("preferred_hour"), int):
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

            score = float(article.ai_score or 0)
            slot, why = plan_slot(session, channel, cfg, verdict,
                                  article.section, fmt, article.title, now, preferred,
                                  score=score, allow_similar=bool(existing))
            late = False
            if slot is None and verdict.latest is not None:
                # queue was full inside the status window — a later slot still
                # beats dropping content the AI decided to publish
                import dataclasses

                slot, why = plan_slot(session, channel, cfg,
                                      dataclasses.replace(verdict, latest=None),
                                      article.section, fmt, article.title, now,
                                      preferred, score=score,
                                      allow_similar=bool(existing))
                late = slot is not None
            if slot is None:
                session.add(Evaluation(article_id=article.id, channel=channel,
                                       outcome="blocked",
                                       reason=f"nav derīga laika: {why}"))
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
                hook_type=str(ch_dec.get("hook_type") or ""),
                link_url=article.canonical_url or article.url,
                scheduled_at=slot, state="scheduled", dry_run=runtime.is_dry_run(session),
            )
            session.add(post)
            session.flush()
            session.add(Evaluation(article_id=article.id, channel=channel,
                                   outcome="posted",
                                   reason=f"scheduled {slot:%Y-%m-%d %H:%M} UTC as {fmt}"
                                          + (" (otrais vilnis)" if existing else "")
                                          + (" (vēlāk — rinda bija pilna)" if late else "")
                                          + (f" (fixes: {', '.join(fixes)})" if fixes else "")))
            created += 1
            scheduled_here += 1
        if scheduled_here == 0:
            requeue_for_retry(article, now)
        session.commit()
    return created


# The queue can be genuinely full when an article is decided. Dropping it
# there wastes editorial work, so such articles are re-decided on later
# cycles until they land — bounded, because freshness rules eventually
# block them anyway.
MAX_DECISION_RETRIES = 8
RETRY_BACKOFF_MINUTES = 20


def retry_pending(article, now) -> bool:
    """True while an article is waiting out its retry backoff."""
    stamp = (article.raw_json or {}).get("_decide_retry_after")
    if not stamp:
        return False
    try:
        return datetime.fromisoformat(str(stamp)) > now
    except ValueError:
        return False


def requeue_for_retry(article, now) -> None:
    raw = dict(article.raw_json or {})
    tries = int(raw.get("_decide_retries") or 0) + 1
    raw["_decide_retries"] = tries
    raw["_decide_retry_after"] = (
        now + timedelta(minutes=RETRY_BACKOFF_MINUTES * tries)).isoformat()
    article.raw_json = raw
    if tries <= MAX_DECISION_RETRIES:
        article.decided_at = None  # picked up again by the next decision run
        log.info("article %s scheduled nowhere (attempt %d) — will retry",
                 article.id, tries)
    else:
        article.decided_at = now
        log.info("article %s scheduled nowhere after %d attempts — giving up",
                 article.id, tries)


def maybe_correct_section(article, decision: dict) -> None:
    """Feed hints mislabel sections (a 'must' feed tagging NATO news as
    entertainment); the AI classifies from content. Sections derived from
    the CMS (term-ID mapping or URL path) are authoritative."""
    sec = decision.get("section") or ""
    if (sec in ("news", "sport", "entertainment") and sec != article.section
            and (article.raw_json or {}).get("_section_src") not in ("terms", "url")):
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


def prebranded(image_url: str) -> bool:
    """True for images that already carry a baked-in headline (photopost
    graphics) — never put the title plate on top of those."""
    patterns = config.load_rules().get("prebranded_image_patterns")
    if patterns is None:
        patterns = ["photopost"]
    return any(p and p in (image_url or "") for p in patterns)


def article_date(article) -> str:
    """dd.mm.yyyy no raksta publicēšanas laika — grafiku datuma čipam."""
    dt = article.published_at or article.first_seen_at
    return dt.strftime("%d.%m.%Y") if dt else ""


def branded_photo(article, image_url: str, platform: str = "") -> str:
    """Photo posts carry the article image with the tv3.lv title plate
    burned in (rules.yaml photo_title_overlay). Falls back to the raw
    image when the renderer is unavailable or fails."""
    from app import cards

    rules = config.load_rules()
    if prebranded(image_url):
        return image_url  # the graphic already has its headline
    if not rules.get("photo_title_overlay", True) or not cards.renderer_available():
        return image_url
    width, height = PHOTO_SIZES.get(platform, (1080, 1080))
    try:
        return cards.render_share_image(article.title, article.section, image_url,
                                        width=width, height=height,
                                        date_txt=article_date(article))
    except Exception as e:  # noqa: BLE001
        log.warning("share image render failed for article %s: %s", article.id, e)
        cards.record_render_failure("photo", e)
        return image_url


def story_media(article, image_url: str) -> list[str]:
    """Vertical story media. An article with a real 9:16 clip becomes a
    VIDEO story (clip + CTA end card); otherwise the branded story image;
    falls back to the raw article image; empty when nothing visual exists."""
    from app import cards, reels

    video = reels.article_video(article)
    if video and reels.available():
        try:
            return [reels.build_video_reel(video,
                                           max_seconds=reels.STORY_MAX_SECONDS)]
        except Exception as e:  # noqa: BLE001
            log.warning("video story failed for article %s: %s", article.id, e)
            cards.record_render_failure("story", e)
    if cards.renderer_available():
        try:
            # a pre-branded source keeps its own headline; we add only the
            # CTA layer (brand chip + poga + tv3.lv) around it
            return [cards.render_story(article.title, article.section, image_url,
                                       with_title=not prebranded(image_url),
                                       date_txt=article_date(article))]
        except Exception as e:  # noqa: BLE001
            log.warning("story render failed for article %s: %s", article.id, e)
            cards.record_render_failure("story", e)
    return [image_url] if image_url else []


def resolve_format(session, channel: str, cfg: dict, article, ch_dec: dict):
    """Format for this post. A carousel happens only when the AI proposed it
    AND provided usable card points AND the renderer works; otherwise the
    diversity-aware chooser decides and media is derived from the article."""
    from app import cards

    ai_fmt = ch_dec.get("format")
    if ai_fmt == "card_carousel" and "card_carousel" in (cfg.get("formats") or []):
        points = [p.strip() for p in (ch_dec.get("card_points") or [])
                  if isinstance(p, str) and p.strip()][:4]
        if len(points) >= 2 and cards.renderer_available():
            tag = "#" + (article.labels[0].upper().replace(" ", "")
                         if article.labels else article.section.upper())
            image = photo_base_image(article)
            # a pre-branded graphic becomes the cover as-is (its headline IS
            # the cover); a clean photo gets our title plate on top
            cover_title = not prebranded(image)
            point_bg = next((img for img in (article.images or [])
                             if img and not prebranded(img)), "")
            question = (ch_dec.get("card_end_question")
                        or "Uzzini visu stāstu tv3.lv").strip()
            try:
                media = cards.render_cards(article.title, article.section, tag,
                                           points, image, question,
                                           cover_title=cover_title,
                                           point_bg=point_bg,
                                           date_txt=article_date(article))
                return "card_carousel", media
            except Exception as e:  # noqa: BLE001 — never lose the post over a render
                log.warning("card render failed for article %s: %s", article.id, e)
                cards.record_render_failure("card_carousel", e)
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
                from app import cards as _cards

                _cards.record_render_failure("video_reel", e)
        points = [p.strip() for p in (ch_dec.get("card_points") or [])
                  if isinstance(p, str) and p.strip()][:3]
        image = photo_base_image(article)
        if prebranded(image):
            image = ""  # reel cover renders its own headline
        if len(points) >= 2 and reels.available():
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
    # own correctly sized branded image there. But not when link posts are
    # below their configured floor — that conversion is what silently turned
    # the whole feed into photo posts, since most tv3.lv og images are the
    # portrait photopost graphic.
    if (fmt == "link" and (article.images or [])
            and "photo" in (cfg.get("formats") or [])
            and config.load_rules().get("portrait_link_to_photo", True)
            and not mix_deficit(recent_format_shares(session, channel),
                                cfg.get("format_mix") or {}, ["link"])):
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


def repost_offset(article, cfg: dict, existing: list) -> datetime | None:
    """When a second post for this article may go out on the channel, or None.

    Deliberate duplication (the competitor pattern: photo first, link post an
    hour later) is reserved for content the AI rated strongly, capped at one
    extra post, and only where the channel configures repost_after_minutes.
    """
    minutes = int(cfg.get("repost_after_minutes") or 0)
    if not minutes or len(existing) != 1:
        return None
    rules = config.load_rules()
    if float(article.ai_score or 0) < float(rules.get("repost_min_score", 0.75)):
        return None
    first = existing[0].scheduled_at
    return first + timedelta(minutes=minutes) if first else None


def compose_text(post, platform: str, shown_link: str,
                 rules: dict | None = None) -> tuple[str, bool]:
    """(post text, whether the link also goes out as the first comment).

    On FB/IG image posts the link goes into the first comment — the
    SocialFlow tactic. It ALSO stays in the caption (rules.yaml
    link_in_caption): one tap for the reader either way, and a caption that
    carries the destination is what Facebook can amplify as a traffic ad.
    Instagram drops it from the caption on its own (links aren't clickable
    there), so only the comment carries it.
    """
    rules = config.load_rules() if rules is None else rules
    in_comment = bool(
        shown_link and platform in ("facebook_page", "instagram")
        and post.format in ("photo", "photo_album", "card_carousel", "reel")
        and rules.get("link_in_first_comment", True))
    in_caption = rules.get("link_in_caption", True) or not in_comment
    text = assemble_post_text(post.copy, post.hashtags or [],
                              shown_link if in_caption else "", platform)
    return text, in_comment


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
            rules = config.load_rules()
            link = (add_utm(post.link_url, platform, post.id, hook=post.hook_type or "")
                    if post.link_url else "")
            # what a reader sees: the tv3.lv short link when one is configured
            # (the full tracked URL still goes to the API as the link target,
            # where only the domain is ever displayed)
            shown = shortlinks.display_link(post.id, link, rules)
            text, first_comment_link = compose_text(post, platform, shown, rules)
            adapter = get_adapter(platform)
            raw_card_links = (post.extra or {}).get("card_links") or []
            card_links = [add_utm(u, platform, post.id, hook=f"karte{i + 1}")
                          if u else "" for i, u in enumerate(raw_card_links)]
            card_titles = (post.extra or {}).get("card_titles") or []
            if (not card_titles and post.format == "card_carousel"
                    and post.article is not None):
                # parastam karuselim FB teksta josla zem katras kartītes rāda
                # raksta virsrakstu — tukša josla izskatās pēc kļūdas
                card_titles = [post.article.title] * len(post.media or [])
            extra_kwargs = {}
            if card_links:
                extra_kwargs["card_links"] = card_links
            if card_titles:
                extra_kwargs["card_titles"] = card_titles
            post.platform_post_id = adapter.publish(
                text=text, link=link, images=post.media or [], fmt=post.format,
                **extra_kwargs)
            post.state = "published"
            post.published_at = utcnow()
            post.error = ""
            published += 1
            if first_comment_link and post.platform_post_id:
                try:
                    adapter.comment(post.platform_post_id, shown)
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
