"""Social media best practice, enforced in code.

The AI writes the copy; this module guarantees it complies with platform
rules regardless of what the model returned. Anything platform-specific
(limits, hashtag policy, link handling) lives here in PLATFORM_SPECS.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl


@dataclass
class PlatformSpec:
    max_chars: int                 # hard platform limit
    ideal_max_chars: int           # best-practice target (engagement drops beyond this)
    max_hashtags: int
    max_emoji: int
    link_char_cost: int | None     # X counts every URL as 23 chars; None = actual length
    link_in_copy: bool             # whether the link goes into the post text
    formats: tuple[str, ...] = field(default_factory=tuple)


PLATFORM_SPECS: dict[str, PlatformSpec] = {
    "facebook_page": PlatformSpec(
        max_chars=5000, ideal_max_chars=400, max_hashtags=1, max_emoji=2,
        link_char_cost=None, link_in_copy=True,
        formats=("link", "photo", "photo_album", "text_only"),
    ),
    "x": PlatformSpec(
        max_chars=280, ideal_max_chars=280, max_hashtags=2, max_emoji=2,
        link_char_cost=23, link_in_copy=True,
        formats=("link", "text_only", "photo"),
    ),
    "threads": PlatformSpec(
        max_chars=500, ideal_max_chars=500, max_hashtags=1, max_emoji=2,
        link_char_cost=None, link_in_copy=True,
        formats=("link", "text_only", "photo"),
    ),
    "instagram": PlatformSpec(
        max_chars=2200, ideal_max_chars=300, max_hashtags=5, max_emoji=3,
        link_char_cost=None, link_in_copy=False,  # links don't work in IG captions
        formats=("photo", "carousel"),
    ),
}

# House style: these read as clickbait/tabloid and are never allowed.
CLICKBAIT_PATTERNS = [
    r"(?i)tu\s+nenoticēsi",
    r"(?i)you\s+won'?t\s+believe",
    r"(?i)\bšoks\b|\bšokējoši\b",
    r"(?i)noklikšķini|klikšķini\s+šeit|click\s+here",
    r"!!+",
]

EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF⬀-⯿←-⇿️]"
)

# Sensitivities where the tone must stay sober: no emoji, no exclamation marks.
SOBER_SENSITIVITIES = {"tragedy", "crime"}


def add_utm(url: str, platform: str, post_id: int | str) -> str:
    """Every outbound link is measurable: utm_content carries the post id."""
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query))
    query.update({
        "utm_source": platform,
        "utm_medium": "social",
        "utm_campaign": "autopilot",
        "utm_content": str(post_id),
    })
    return urlunparse(parsed._replace(query=urlencode(query)))


def effective_length(text: str, spec: PlatformSpec) -> int:
    """Length as the platform counts it (X: URLs cost 23 chars each)."""
    if spec.link_char_cost is None:
        return len(text)
    url_re = re.compile(r"https?://\S+")
    stripped = url_re.sub("", text)
    n_urls = len(url_re.findall(text))
    return len(stripped) + n_urls * spec.link_char_cost


def _truncate_to(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:.") + "…"


def sanitize_copy(copy: str, hashtags: list[str], platform: str,
                  sensitivity: list[str] | None = None,
                  reserve_link_chars: bool = False) -> tuple[str, list[str], list[str]]:
    """Enforce house style + platform limits. Returns (copy, hashtags, applied_fixes)."""
    spec = PLATFORM_SPECS.get(platform) or PLATFORM_SPECS["facebook_page"]
    fixes: list[str] = []
    sensitivity = sensitivity or []

    copy = copy.strip()

    for pat in CLICKBAIT_PATTERNS:
        if re.search(pat, copy):
            copy = re.sub(pat, "", copy).strip(" ,.-")
            fixes.append("removed clickbait phrase")

    # ALL-CAPS words (≥4 letters, allow acronyms up to 3) read as shouting.
    def _decap(m: re.Match) -> str:
        return m.group(0).capitalize()
    new_copy = re.sub(r"\b[A-ZĀČĒĢĪĶĻŅŠŪŽ]{4,}\b", _decap, copy)
    if new_copy != copy:
        copy, _ = new_copy, fixes.append("lowercased shouting")

    if any(s in SOBER_SENSITIVITIES for s in sensitivity):
        stripped = EMOJI_RE.sub("", copy).replace("!", ".")
        if stripped != copy:
            copy, _ = stripped.strip(), fixes.append("sober tone: removed emoji/exclamations")
        hashtags = []
    else:
        emojis = EMOJI_RE.findall(copy)
        if len(emojis) > spec.max_emoji:
            for e in emojis[spec.max_emoji:]:
                copy = copy.replace(e, "", 1)
            fixes.append(f"kept only {spec.max_emoji} emoji")
        if copy.count("?") > 1:
            first = copy.find("?")
            copy = copy[: first + 1] + copy[first + 1:].replace("?", ".")
            fixes.append("max one question")

    hashtags = [h if h.startswith("#") else f"#{h}" for h in hashtags if h and h.strip("#")]
    if len(hashtags) > spec.max_hashtags:
        hashtags = hashtags[: spec.max_hashtags]
        fixes.append(f"kept only {spec.max_hashtags} hashtags")

    budget = spec.max_chars
    if reserve_link_chars and spec.link_in_copy:
        budget -= (spec.link_char_cost or 30) + 1  # link + separating space
    tags_len = sum(len(h) + 1 for h in hashtags)
    limit = min(budget - tags_len, spec.ideal_max_chars)
    if effective_length(copy, spec) > limit:
        copy = _truncate_to(copy, limit)
        fixes.append(f"truncated to {limit} chars")

    return copy.strip(), hashtags, fixes


def assemble_post_text(copy: str, hashtags: list[str], link: str, platform: str) -> str:
    spec = PLATFORM_SPECS.get(platform) or PLATFORM_SPECS["facebook_page"]
    parts = [copy]
    if hashtags:
        parts.append(" ".join(hashtags))
    if link and spec.link_in_copy:
        parts.append(link)
    return "\n\n".join(p for p in parts if p)


# Hour-of-day engagement prior (Europe/Riga). Values are relative weights used
# by the slot allocator until real measured data replaces them (Phase 3).
# Peaks: morning commute/coffee, lunch, and the evening prime window.
DEFAULT_HOUR_WEIGHTS = {
    0: 0.1, 1: 0.05, 2: 0.05, 3: 0.05, 4: 0.05, 5: 0.1,
    6: 0.4, 7: 0.8, 8: 0.9, 9: 0.7, 10: 0.6, 11: 0.7,
    12: 0.9, 13: 0.8, 14: 0.6, 15: 0.6, 16: 0.7, 17: 0.8,
    18: 0.8, 19: 1.0, 20: 1.0, 21: 0.9, 22: 0.6, 23: 0.3,
}


def pick_format(article_section: str, images: list, editor_status: str,
                allowed_formats: list[str] | tuple[str, ...]) -> str:
    """Best-practice default format when the AI doesn't decide (fallback path).

    Link posts get the best CTR to the site; photo albums only for strong
    galleries; sport favours short text+link.
    """
    allowed = list(allowed_formats) or ["link"]
    if "link" in allowed and (article_section in ("news", "sport") or not images):
        return "link"
    if "photo_album" in allowed and len(images) >= 4:
        return "photo_album"
    if "photo" in allowed and images and article_section == "entertainment":
        return "photo"
    return "link" if "link" in allowed else allowed[0]
