"""Reklāmu kreatīvu sagatave, kas nav platformas API lieta.

Google (Demand Gen, Display) vienu attēlu grib trīs proporcijās un logo
kvadrātā; teksti ir stingri limitēti rakstzīmēs un griežami pie vārda
robežas. Meta to visu dara pati, tāpēc šis modulis kalpo Google pusei, bet
neko Google-specifisku nezina — tikai attēli un teksts.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Google prasītās proporcijas: 1.91:1, 1:1, 4:5 (portrets tikai Demand Gen)
SIZES = {"landscape": (1200, 628), "square": (1200, 1200), "portrait": (960, 1200)}
LOGO_PATH = Path(__file__).resolve().parent.parent / "branding/assets/tv3lv_logo_card.png"


def fit(text: str, limit: int) -> str:
    """Teksts līdz `limit` rakstzīmēm, griezts pie vārda robežas, bez
    nogrieztas pieturzīmes astes."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,;:–-—") or text[:limit]


def _read(image: str) -> bytes:
    from adapters.facebook import FacebookPageAdapter

    return FacebookPageAdapter._image_bytes(image)


def image_variants(image: str, need_portrait: bool = True) -> dict[str, bytes]:
    """{landscape, square[, portrait]} -> PNG baiti no viena avota attēla.

    Bez renderētāja atgriež tikai oriģinālu kā `landscape` — Google to
    pieņems, ja proporcija sakrīt, un noraidīs, ja ne; labāk viens mēģinājums
    nekā neviens.
    """
    from app import cards

    out: dict[str, bytes] = {}
    if cards.renderer_available():
        for key, (w, h) in SIZES.items():
            if key == "portrait" and not need_portrait:
                continue
            try:
                path = cards.render_crop(image, w, h)
                out[key] = Path(path).read_bytes()
                Path(path).unlink(missing_ok=True)
            except Exception as e:  # noqa: BLE001 — viens izmērs nedrīkst nogāzt visus
                log.warning("ad crop %s failed for %s: %s", key, image[:60], e)
    if not out:
        try:
            out["landscape"] = _read(image)
        except Exception as e:  # noqa: BLE001
            log.warning("ad image read failed for %s: %s", image[:60], e)
    return out


def logo_square() -> bytes:
    """Kvadrātisks logo uz balta fona (Google logoImages prasa 1:1)."""
    from app import cards

    if cards.renderer_available():
        try:
            path = cards.render_crop(str(LOGO_PATH), 1200, 1200, contain=True)
            data = Path(path).read_bytes()
            Path(path).unlink(missing_ok=True)
            return data
        except Exception as e:  # noqa: BLE001
            log.warning("logo render failed: %s", e)
    return LOGO_PATH.read_bytes() if LOGO_PATH.exists() else b""
