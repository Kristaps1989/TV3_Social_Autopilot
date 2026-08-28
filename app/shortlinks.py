"""tv3.lv-branded short links: tv3.lv/r/<kods> -> raksts ar UTM.

A social post's visible URL is a branding surface. The full tracked link
(long slug + five utm parameters) eats three lines of a Facebook caption;
competitors show a clean one-line URL. So the copy carries a short code and
this app resolves it: /r/<code> -> 302 -> the very same UTM link publishing
would have used, so GA4 attribution is untouched.

The code is derived from the post id (no extra state, no lookup table); the
redirect is the one place that also counts real clicks — Facebook reports no
click metric for photo posts and Instagram none at all.
"""
from __future__ import annotations

# no 0/o/1/l/i — a short link ends up being read aloud and typed by hand
ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
BASE = len(ALPHABET)
# codes start at 3 characters and don't advertise how many posts exist
OFFSET = BASE * BASE

BOT_MARKERS = ("bot", "crawler", "spider", "facebookexternalhit", "preview",
               "slurp", "fetch", "monitor", "curl", "wget", "python-httpx")


def encode(post_id: int) -> str:
    n = int(post_id) + OFFSET
    out = ""
    while n:
        n, rem = divmod(n, BASE)
        out = ALPHABET[rem] + out
    return out


def decode(code: str) -> int | None:
    n = 0
    for ch in (code or "").strip().lower():
        idx = ALPHABET.find(ch)
        if idx < 0:
            return None
        n = n * BASE + idx
    post_id = n - OFFSET
    return post_id if post_id > 0 else None


def base_url(rules: dict | None = None) -> str:
    """Configured short-link prefix, e.g. https://tv3.lv/r ('' = disabled,
    links then go out in full)."""
    from app import config

    rules = config.load_rules() if rules is None else rules
    return str((rules or {}).get("short_link_base") or "").strip().rstrip("/")


def short_url(post_id: int, rules: dict | None = None) -> str:
    base = base_url(rules)
    return f"{base}/{encode(post_id)}" if base else ""


def display_link(post_id: int, full_url: str, rules: dict | None = None) -> str:
    """What a human sees in the caption or first comment: the short link when
    one is configured, otherwise the full tracked URL."""
    if not full_url:
        return ""
    return short_url(post_id, rules) or full_url


def is_bot(user_agent: str) -> bool:
    ua = (user_agent or "").lower()
    return not ua or any(m in ua for m in BOT_MARKERS)
