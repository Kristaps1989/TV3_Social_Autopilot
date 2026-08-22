"""Carousel card renderer — turns an article + AI-extracted highlights into
branded 1080x1080 cards (cover, content cards, end card with CTA).

Design follows the tv3.lv section banner system: section color, white
swoosh, tv3.lv logo, /SECTION #TAG label. Rendered with headless Chromium;
if Chromium is unavailable the pipeline falls back to photo/link formats.
"""
from __future__ import annotations

import html
import logging
import os
import secrets
from pathlib import Path

from app import config

log = logging.getLogger(__name__)

CARDS_DIR = Path(os.environ.get("CARDS_DIR", "data/cards"))

SECTION_STYLE = {
    "news": {"label": "ZIŅAS", "color": "#a5495a", "kicker": "SKAIDROJUMS"},
    "sport": {"label": "SPORTS", "color": "#3d6e94", "kicker": "SPORTS"},
    "entertainment": {"label": "IZKLAIDE", "color": "#8e4f8e", "kicker": "IZKLAIDE"},
}

# Official tv3.lv logo (uploaded by TV3, margins trimmed for layout use).
_LOGO_PATH = Path(__file__).resolve().parent.parent / "branding/assets/tv3lv_logo_card.png"
_logo_data_uri: str | None = None


def _logo(height: int) -> str:
    global _logo_data_uri
    if _logo_data_uri is None:
        import base64

        _logo_data_uri = ("data:image/png;base64,"
                          + base64.b64encode(_LOGO_PATH.read_bytes()).decode())
    return f'<img src="{_logo_data_uri}" style="height:{height}px" alt="tv3.lv">'


def _shade(color: str, delta: float) -> str:
    import colorsys

    r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    l = min(1, max(0, l + delta))
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def renderer_available() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    chromium = os.environ.get("PLAYWRIGHT_CHROMIUM", "")
    if chromium and not Path(chromium).exists():
        return False
    return True


# The white corner swoosh is the sponsor area ("SADARBĪBĀ AR ...").
# tv3.lv has no article sponsors right now, so it is off by default;
# flip this on (or make it per-post) when sponsorships come back.
SHOW_SPONSOR = os.environ.get("CARD_SPONSOR", "").lower() == "true"


def build_cards_html(title: str, section: str, tag: str, points: list[str],
                     image_url: str, end_question: str,
                     show_sponsor: bool | None = None) -> str:
    style = SECTION_STYLE.get(section) or SECTION_STYLE["news"]
    color = style["color"]
    dark = _shade(color, -0.18)
    total = len(points) + 2
    esc = html.escape

    if show_sponsor is None:
        show_sponsor = SHOW_SPONSOR
    swoosh = ""
    if show_sponsor:
        swoosh = ('<svg class="sw" viewBox="0 0 1080 810" preserveAspectRatio="none">'
                  '<path d="M 800,0 C 716,6 688,34 699,82 C 728,188 908,290 1080,332 '
                  'L 1080,0 Z" fill="#fff"/></svg>'
                  '<div class="sad">SADARBĪBĀ AR</div>')

    def bar(page: int) -> str:
        return f"""<div class="bar">{_logo(88)}
          <div class="lbl"><div class="sec"><span>/</span>{esc(style['label'])}</div>
          <div class="tag">{esc(tag)}</div></div></div>"""

    cover_bg = (f'background:url({html.escape(image_url, quote=True)}) center/cover, {color};'
                if image_url else f"background:{color};")
    # the darkening gradient exists to keep the headline readable over a
    # photo; on a flat color card it just muddies the brand color
    shade = '<div class="shade"></div>' if image_url else ""
    cards = [f"""
    <div class="card">
      <div class="art" style="{cover_bg}">
        {shade}{swoosh}
        <div class="page">1/{total} →</div>
        <div class="cover-txt">
          <div class="kicker" style="background:{dark}">{esc(style['kicker'])}</div>
          <h1>{esc(title)}</h1>
        </div>
      </div>{bar(1)}
    </div>"""]

    for n, point in enumerate(points, start=1):
        cards.append(f"""
    <div class="card">
      <div class="art" style="background:{color}">
        {swoosh}
        <div class="page">{n + 1}/{total} →</div>
        <div class="point">
          <div class="num" style="color:{dark}">{n}</div>
          <p>{esc(point)}</p>
        </div>
        <div class="dots">{''.join('<i class="on"></i>' if i == n else '<i></i>'
                                    for i in range(total))}</div>
      </div>{bar(n + 1)}
    </div>""")

    cards.append(f"""
    <div class="card">
      <div class="art end" style="background:{color}">
        <div class="endlogo">{_logo(150)}</div>
        <h2>{esc(end_question)}</h2>
        <div class="cta" style="color:{color}">Lasi pilno rakstu →</div>
        <div class="url">tv3.lv</div>
      </div>
    </div>""")

    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
* {{ margin:0; box-sizing:border-box; font-family:"DejaVu Sans",sans-serif; }}
.card {{ width:1080px; height:1080px; overflow:hidden; display:flex;
        flex-direction:column; background:#fff; }}
.art {{ position:relative; height:940px; overflow:hidden; flex:none; }}
.shade {{ position:absolute; inset:0;
  background:linear-gradient(to top, rgba(10,5,15,.92) 18%, rgba(10,5,15,.1) 55%); }}
.sw {{ position:absolute; top:0; right:0; width:100%; height:100%; }}
.sad {{ position:absolute; top:44px; right:40px; color:#111; font-size:26px;
        letter-spacing:.06em; }}
.page {{ position:absolute; top:44px; left:48px; color:#fff; font-size:30px;
         opacity:.9; font-weight:bold; }}
.cover-txt {{ position:absolute; bottom:0; left:0; right:0; padding:56px;
              color:#fff; }}
.kicker {{ display:inline-block; color:#fff; font-weight:bold; font-size:28px;
           letter-spacing:.12em; padding:10px 24px; border-radius:8px;
           margin-bottom:26px; }}
h1 {{ font-size:66px; line-height:1.14; font-weight:bold; }}
.point {{ position:absolute; inset:0; display:flex; flex-direction:column;
          justify-content:center; padding:0 88px; color:#fff; }}
.num {{ font-size:150px; font-weight:bold; line-height:1; margin-bottom:34px; }}
.point p {{ font-size:54px; line-height:1.3; font-weight:bold; }}
.dots {{ position:absolute; bottom:44px; right:48px; display:flex; gap:13px; }}
.dots i {{ width:17px; height:17px; border-radius:50%;
           background:rgba(255,255,255,.35); }}
.dots i.on {{ background:#fff; }}
.bar {{ height:140px; background:#fff; display:flex; align-items:center;
        justify-content:space-between; padding:0 48px; flex:none; }}
.lbl {{ text-align:right; line-height:1.15; }}
.sec {{ color:#111; font-weight:600; font-size:30px; }}
.sec span {{ color:#f01414; font-weight:800; }}
.tag {{ font-weight:800; font-size:44px; }}
.card:nth-child(n) .tag {{ color:{color}; }}
.end {{ height:1080px; display:flex; flex-direction:column; align-items:center;
        justify-content:center; text-align:center; padding:80px; color:#fff; }}
.endlogo {{ background:#fff; border-radius:24px; padding:34px 60px;
            margin-bottom:64px; }}
h2 {{ font-size:56px; line-height:1.28; margin-bottom:64px; max-width:860px; }}
.cta {{ background:#fff; font-size:42px; font-weight:bold; padding:28px 66px;
        border-radius:99px; }}
.url {{ margin-top:44px; font-size:34px; opacity:.85; font-weight:bold; }}
</style></head><body>{''.join(cards)}</body></html>"""


def build_share_html(title: str, section: str, image_url: str,
                     kicker: str = "") -> str:
    """Single branded share image (1080x1080): article photo full-bleed with
    the tv3.lv title plate — white box, bold headline, red accent bar —
    matching the tv3.lv site's own share-image style."""
    style = SECTION_STYLE.get(section) or SECTION_STYLE["news"]
    color = style["color"]
    esc = html.escape
    bg = (f'background:url({html.escape(image_url, quote=True)}) center/cover, {color};'
          if image_url else f"background:{color};")
    kicker_html = (f'<div class="kick">{esc(kicker)}</div>' if kicker else "")
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
* {{ margin:0; box-sizing:border-box; font-family:"DejaVu Sans",sans-serif; }}
.share {{ width:1080px; height:1080px; position:relative; overflow:hidden; {bg} }}
.plate {{ position:absolute; left:0; bottom:96px; max-width:900px;
          background:#fff; padding:44px 56px 44px 48px;
          border-right:18px solid #e3000f;
          box-shadow:0 10px 40px rgba(0,0,0,.25); }}
.kick {{ display:inline-block; background:#e3000f; color:#fff; font-weight:bold;
         font-size:26px; letter-spacing:.1em; padding:8px 18px;
         position:absolute; top:-46px; left:0; }}
.plate h1 {{ font-size:56px; line-height:1.16; font-weight:bold; color:#111; }}
.brand {{ position:absolute; right:36px; bottom:34px; background:#fff;
          border-radius:12px; padding:12px 20px; }}
</style></head><body>
<div class="share">
  <div class="plate">{kicker_html}<h1>{esc(title)}</h1></div>
  <div class="brand">{_logo(44)}</div>
</div>
</body></html>"""


def render_share_image(title: str, section: str, image_url: str,
                       kicker: str = "", out_dir: Path | None = None) -> str:
    from playwright.sync_api import sync_playwright

    out_dir = out_dir or CARDS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(6)
    tmp = out_dir / f"_s{token}.html"
    tmp.write_text(build_share_html(title, section, image_url, kicker),
                   encoding="utf-8")
    chromium = os.environ.get("PLAYWRIGHT_CHROMIUM", "")
    try:
        with sync_playwright() as p:
            browser = (p.chromium.launch(executable_path=chromium) if chromium
                       else p.chromium.launch())
            page = browser.new_page(viewport={"width": 1080, "height": 1080})
            page.goto(tmp.as_uri(), timeout=30000)
            page.wait_for_timeout(800)  # let the article photo load
            out = out_dir / f"share_{token}.png"
            page.locator(".share").screenshot(path=str(out), timeout=15000)
            browser.close()
    finally:
        tmp.unlink(missing_ok=True)
    return str(out)


def render_cards(title: str, section: str, tag: str, points: list[str],
                 image_url: str, end_question: str,
                 out_dir: Path | None = None) -> list[str]:
    """Render carousel cards to PNG files; returns local file paths."""
    from playwright.sync_api import sync_playwright

    out_dir = out_dir or CARDS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    html_doc = build_cards_html(title, section, tag, points, image_url, end_question)
    token = secrets.token_hex(6)
    tmp = out_dir / f"_{token}.html"
    tmp.write_text(html_doc, encoding="utf-8")
    paths: list[str] = []
    chromium = os.environ.get("PLAYWRIGHT_CHROMIUM", "")
    try:
        with sync_playwright() as p:
            browser = (p.chromium.launch(executable_path=chromium) if chromium
                       else p.chromium.launch())
            page = browser.new_page(viewport={"width": 1080, "height": 1080})
            page.goto(tmp.as_uri(), timeout=30000)
            page.wait_for_timeout(600)  # give the cover image a moment to load
            for i, el in enumerate(page.locator(".card").all()):
                f = out_dir / f"card_{token}_{i}.png"
                el.screenshot(path=str(f), timeout=15000)
                paths.append(str(f))
            browser.close()
    finally:
        tmp.unlink(missing_ok=True)
    return paths
