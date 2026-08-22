"""Diversity-aware format selection.

Picks the best format for a specific post instead of defaulting to link:
  score = channel weight (channels.yaml format_weights, editor-tunable)
        × suitability for THIS article (does it have images? a gallery?)
        × feed-diversity multiplier (formats overused in the channel's last
          posts are discounted, underused ones boosted)
  and the AI's explicit choice gets a bonus so it wins unless the feed is
  already saturated with that format.

Once Phase 3 metrics exist, format_weights get replaced by measured
sessions-per-post — the mechanism stays the same.
"""
from __future__ import annotations

from sqlalchemy import select

from app.models import Article, Post

# Fallback weights when a channel doesn't configure format_weights.
DEFAULT_FORMAT_WEIGHTS = {
    "link": 1.0, "photo": 0.9, "photo_album": 0.8,
    "text_only": 0.6, "carousel": 0.7, "video": 0.9,
}

DIVERSITY_WINDOW = 6
AI_CHOICE_BONUS = 1.25


def suitable_formats(article: Article, allowed: list[str]) -> list[str]:
    images = article.images or []
    out = []
    for fmt in allowed:
        if fmt == "card_carousel":
            # only the AI proposes carousels (needs card_points); the
            # diversity engine never forces one — handled in the pipeline
            continue
        if fmt == "photo" and not images:
            continue
        if fmt == "photo_album" and len(images) < 4:
            continue
        if fmt == "carousel" and len(images) < 2:
            continue
        if fmt == "video":  # native video is phase 2+
            continue
        if fmt == "link" and not (article.canonical_url or article.url):
            continue
        out.append(fmt)
    return out or (["link"] if article.url else ["text_only"])


def recent_format_shares(session, channel: str) -> dict[str, float]:
    rows = session.execute(
        select(Post.format).where(Post.channel == channel,
                                  Post.state.in_(("scheduled", "publishing", "published")))
        .order_by(Post.created_at.desc()).limit(DIVERSITY_WINDOW)
    ).scalars().all()
    if not rows:
        return {}
    return {f: rows.count(f) / len(rows) for f in set(rows)}


def choose_format(session, channel: str, channel_cfg: dict, article: Article,
                  ai_choice: str | None = None) -> str:
    allowed = list(channel_cfg.get("formats") or ["link"])
    candidates = suitable_formats(article, allowed)
    if len(candidates) == 1:
        return candidates[0]

    weights = {**DEFAULT_FORMAT_WEIGHTS, **(channel_cfg.get("format_weights") or {})}
    shares = recent_format_shares(session, channel)

    # measured sessions-per-post adjusts the configured weights (priors.py)
    from app import priors

    measured = priors.format_multipliers(session, channel)

    # Visual stories lean photo; hard news leans link (best CTR to the site).
    section_bias = {}
    if article.section == "entertainment" and (article.images or []):
        section_bias["photo"] = 1.15
        section_bias["photo_album"] = 1.15
    elif article.section in ("news", "sport"):
        section_bias["link"] = 1.1

    best, best_score = candidates[0], -1.0
    for fmt in candidates:
        score = float(weights.get(fmt, 0.5))
        score *= measured.get(fmt, 1.0)
        score *= section_bias.get(fmt, 1.0)
        score *= 1.3 - shares.get(fmt, 0.0)  # unused 1.3x .. saturated 0.3x
        if fmt == ai_choice:
            score *= AI_CHOICE_BONUS
        if score > best_score:
            best, best_score = fmt, score
    return best
