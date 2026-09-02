"""Lightweight image dimension probe — header parsing only, no image libs.

Used to detect portrait og-images: Facebook crops link-card images to
~1.91:1, so a vertical source image loses its baked-in title plate. The
pipeline switches such articles to photo format (where we render our own
correctly-sized branded image) instead.
"""
from __future__ import annotations

import logging
import struct

import httpx

log = logging.getLogger(__name__)

_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def parse_dimensions(buf: bytes) -> tuple[int, int] | None:
    """(width, height) from PNG/JPEG/GIF/WebP header bytes, else None."""
    if len(buf) < 12:
        return None
    if buf[:8] == b"\x89PNG\r\n\x1a\n" and len(buf) >= 24:
        w, h = struct.unpack(">II", buf[16:24])
        return (w, h)
    if buf[:6] in (b"GIF87a", b"GIF89a") and len(buf) >= 10:
        w, h = struct.unpack("<HH", buf[6:10])
        return (w, h)
    if buf[:2] == b"\xff\xd8":  # JPEG: scan segments for a SOF frame
        i = 2
        while i + 9 < len(buf):
            if buf[i] != 0xFF:
                i += 1
                continue
            marker = buf[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            if marker in _SOF_MARKERS:
                h, w = struct.unpack(">HH", buf[i + 5:i + 9])
                return (w, h)
            seg_len = struct.unpack(">H", buf[i + 2:i + 4])[0]
            i += 2 + seg_len
        return None
    if buf[:4] == b"RIFF" and buf[8:12] == b"WEBP" and len(buf) >= 30:
        chunk = buf[12:16]
        if chunk == b"VP8X":
            w = int.from_bytes(buf[24:27], "little") + 1
            h = int.from_bytes(buf[27:30], "little") + 1
            return (w, h)
        if chunk == b"VP8 ":
            w = struct.unpack("<H", buf[26:28])[0] & 0x3FFF
            h = struct.unpack("<H", buf[28:30])[0] & 0x3FFF
            return (w, h)
    return None


def probe_size(url: str, max_bytes: int = 262144) -> tuple[int, int] | None:
    """Fetch just enough of the image to read its dimensions."""
    if not url or not url.startswith("http"):
        return None
    try:
        with httpx.stream("GET", url, timeout=10, follow_redirects=True,
                          headers={"User-Agent": "TV3-Social-Autopilot/1.0"}) as resp:
            if resp.status_code != 200:
                return None
            buf = b""
            for chunk in resp.iter_bytes(chunk_size=16384):
                buf += chunk
                size = parse_dimensions(buf)
                if size:
                    return size
                if len(buf) >= max_bytes:
                    break
            return parse_dimensions(buf)
    except Exception as e:  # noqa: BLE001
        log.debug("image probe failed for %s: %s", url, e)
        return None


def image_size(article, url: str) -> tuple[int, int] | None:
    """(width, height) for one of the article's images, cached per URL on
    the article's raw_json so every image is probed at most once."""
    if not url:
        return None
    raw = article.raw_json or {}
    dims = dict(raw.get("_img_dims") or {})
    if url in dims:
        wh = dims[url]
    else:
        size = probe_size(url)
        wh = list(size) if size else []
        dims[url] = wh
        article.raw_json = {**raw, "_img_dims": dims}
    return (wh[0], wh[1]) if wh else None


def is_portrait(article, url: str) -> bool:
    size = image_size(article, url)
    return bool(size and size[1] > size[0] * 1.05)


# Cik plats attēls ir «plats»: 1080×940 vāka laukā (1.15:1) tikai šāds foto
# tiek griezts vienīgi sānos — kvadrātu vai 4:5 griež pa vertikāli, un tad
# pazūd galvas
WIDE_ASPECT = 1.2


def is_wide(article, url: str) -> bool | None:
    """True/False pēc izmērītās proporcijas, None — ja izmēru nolasīt neizdodas."""
    size = image_size(article, url)
    if not size or not size[1]:
        return None
    return size[0] / size[1] >= WIDE_ASPECT


def wide_image(article, limit: int = 6) -> str:
    """Pirmais PLATAIS raksta attēls ('' ja nav) — vākam un foto ierakstam
    tāds nekad nezaudē augšu."""
    for url in (article.images or [])[:limit]:
        if is_wide(article, url):
            return url
    return ""


def landscape_image(article, limit: int = 6) -> str:
    """First non-portrait image among the article's images ('' if none).
    tv3.lv lead images are often vertical 'photopost' graphics with a
    headline already baked in; when the feed also carries the original
    horizontal photo it is the better base for our own title plate."""
    for url in (article.images or [])[:limit]:
        size = image_size(article, url)
        if size and not size[1] > size[0] * 1.05:
            return url
    return ""


# Facebook saites kartītes attēla malu attiecība. Šaurāku (augstāku) attēlu
# tā apgriež pa vertikāli — un ziņu fotogrāfijā galvas ir augšējā trešdaļā,
# tāpēc tieši tās nogriež pirmās.
LINK_CARD_ASPECT = 1.91


def link_card_crop(article, url: str) -> float:
    """Cik lielu daļu no attēla AUGSTUMA Facebook saites kartīte nogriezīs.

    0.0 = neko (attēls jau ir 1.91:1 vai platāks), 0.30 = trešdaļu. Ja izmēru
    nolasīt neizdodas, atgriežam 0.0: neziņa nav iemesls mainīt formātu.
    """
    size = image_size(article, url)
    if not size or not size[1]:
        return 0.0
    aspect = size[0] / size[1]
    if aspect >= LINK_CARD_ASPECT:
        return 0.0
    return 1.0 - aspect / LINK_CARD_ASPECT


def orientation(article) -> str | None:
    """'portrait' | 'landscape' | None for the article's lead image.
    Result is cached on the article's raw_json to avoid re-fetching."""
    raw = article.raw_json or {}
    wh = raw.get("_img_wh")
    if wh is None:
        image = (article.images or [None])[0]
        if not image:
            return None
        size = probe_size(image)
        wh = list(size) if size else []
        article.raw_json = {**raw, "_img_wh": wh}
    if not wh:
        return None
    w, h = wh
    return "portrait" if h > w * 1.05 else "landscape"
