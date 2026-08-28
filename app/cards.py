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
from datetime import datetime
from pathlib import Path

from app import config

log = logging.getLogger(__name__)

# Absolute path is load-bearing: Chromium opens the rendered HTML via a
# file:// URI, and Path.as_uri() raises on relative paths ("relative path
# can't be expressed as a file URI") — which silently killed every render
# in production while tests (absolute tmp dirs) kept passing.
CARDS_DIR = Path(os.environ.get("CARDS_DIR", "data/cards")).resolve()

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


_render_check: tuple[float, bool, str] | None = None


def renderer_check(max_age: float = 600.0) -> tuple[bool, str]:
    """(works, error) — actually launches Chromium once (cached). A passing
    import alone does not prove the renderer works; a failing launch silently
    downgrades photo/story posts to the raw article image."""
    global _render_check
    import time

    now = time.time()
    if _render_check and now - _render_check[0] < max_age:
        return _render_check[1], _render_check[2]
    if not renderer_available():
        _render_check = (now, False, "playwright/Chromium nav instalēts")
        return False, _render_check[2]
    try:
        # the real pipeline, not just a launch: render an actual share image
        out = render_share_image("Diagnostikas tests", "news", "",
                                 width=200, height=200)
        Path(out).unlink(missing_ok=True)
        _render_check = (now, True, "")
    except Exception as e:  # noqa: BLE001
        _render_check = (now, False, f"{type(e).__name__}: {str(e)[:300]}")
    return _render_check[1], _render_check[2]


def record_render_failure(context: str, error: Exception) -> None:
    """Persist the latest real render failure so the Konti page can show it
    (scheduler-thread failures are otherwise visible only in server logs)."""
    try:
        CARDS_DIR.mkdir(parents=True, exist_ok=True)
        (CARDS_DIR / "last_render_error.txt").write_text(
            f"{datetime.utcnow():%Y-%m-%d %H:%M} UTC · {context}: "
            f"{type(error).__name__}: {str(error)[:400]}", encoding="utf-8")
    except OSError:
        pass


def last_render_failure() -> str:
    try:
        path = CARDS_DIR / "last_render_error.txt"
        return path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        return ""


# The white corner swoosh is the sponsor area ("SADARBĪBĀ AR ...").
# tv3.lv has no article sponsors right now, so it is off by default;
# flip this on (or make it per-post) when sponsorships come back.
SHOW_SPONSOR = os.environ.get("CARD_SPONSOR", "").lower() == "true"


def build_cards_html(title: str, section: str, tag: str, points: list[str],
                     image_url: str, end_question: str,
                     show_sponsor: bool | None = None,
                     cover_title: bool = True, point_bg: str = "") -> str:
    """cover_title=False: the cover image is a pre-branded graphic that
    already carries the headline — show it full-bleed without our plate.
    point_bg: photo used as a dimmed background on the content cards so the
    gallery is visual, not flat color blocks."""
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
    shade = '<div class="shade"></div>' if image_url and cover_title else ""
    cover_txt = (f"""<div class="cover-txt">
          <div class="kicker" style="background:{dark}">{esc(style['kicker'])}</div>
          <h1>{esc(title)}</h1>
        </div>""" if cover_title else "")
    cards = [f"""
    <div class="card">
      <div class="art" style="{cover_bg}">
        {shade}{swoosh}
        <div class="page">1/{total} →</div>
        {cover_txt}
      </div>{bar(1)}
    </div>"""]

    if point_bg:
        point_style = (f'background:url({html.escape(point_bg, quote=True)}) '
                       f'center/cover, {color};')
        point_shade = '<div class="pshade"></div>'
        num_color = "rgba(255,255,255,.45)"
    else:
        point_style = f"background:{color};"
        point_shade = ""
        num_color = dark
    for n, point in enumerate(points, start=1):
        cards.append(f"""
    <div class="card">
      <div class="art" style="{point_style}">
        {point_shade}{swoosh}
        <div class="page">{n + 1}/{total} →</div>
        <div class="point">
          <div class="num" style="color:{num_color}">{n}</div>
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
.pshade {{ position:absolute; inset:0;
  background:linear-gradient(160deg, rgba(12,6,16,.82) 0%, rgba(12,6,16,.6) 100%); }}
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
                     kicker: str = "", width: int = 1080,
                     height: int = 1080) -> str:
    """Single branded share image: article photo full-bleed with the tv3.lv
    title plate — white box, bold headline, red accent bar — matching the
    tv3.lv site's own share-image style. Size per platform: FB feed 4:5
    (1080x1350, max feed real estate), X/Threads 1:1 (1080x1080, no crop)."""
    style = SECTION_STYLE.get(section) or SECTION_STYLE["news"]
    color = style["color"]
    esc = html.escape
    bg = (f'background:url({html.escape(image_url, quote=True)}) center/cover, {color};'
          if image_url else f"background:{color};")
    kicker_html = (f'<div class="kick">{esc(kicker)}</div>' if kicker else "")
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
* {{ margin:0; box-sizing:border-box; font-family:"DejaVu Sans",sans-serif; }}
.share {{ width:{width}px; height:{height}px; position:relative; overflow:hidden; {bg} }}
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
                       kicker: str = "", out_dir: Path | None = None,
                       width: int = 1080, height: int = 1080) -> str:
    from playwright.sync_api import sync_playwright

    out_dir = Path(out_dir or CARDS_DIR).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(6)
    tmp = out_dir / f"_s{token}.html"
    tmp.write_text(build_share_html(title, section, image_url, kicker,
                                    width, height),
                   encoding="utf-8")
    chromium = os.environ.get("PLAYWRIGHT_CHROMIUM", "")
    try:
        with sync_playwright() as p:
            browser = (p.chromium.launch(executable_path=chromium) if chromium
                       else p.chromium.launch())
            page = browser.new_page(viewport={"width": width, "height": height})
            page.goto(tmp.as_uri(), timeout=30000)
            page.wait_for_timeout(800)  # let the article photo load
            out = out_dir / f"share_{token}.png"
            page.locator(".share").screenshot(path=str(out), timeout=15000)
            browser.close()
    finally:
        tmp.unlink(missing_ok=True)
    return str(out)


# Stories publicētas caur API nevar saturēt klikšķējamu link sticker
# (Page Stories API pieņem tikai photo_id/video_id), tāpēc CTA vizuāli
# atdarina link sticker ar redzamu domēnu — skatītājs zina, kur iet.
_LINK_ICON = (
    '<svg width="46" height="46" viewBox="0 0 24 24" fill="none" '
    'stroke="#e3000f" stroke-width="2.4" stroke-linecap="round" '
    'stroke-linejoin="round">'
    '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/>'
    '<path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>'
    '</svg>')


def build_story_html(title: str, section: str, image_url: str,
                     kicker: str = "", with_title: bool = True) -> str:
    """Vertical story (1080x1920) in the tv3.lv style: full-bleed image,
    title plate, CTA. Top/bottom safe zones respected (platform UI)."""
    style = SECTION_STYLE.get(section) or SECTION_STYLE["news"]
    color = style["color"]
    esc = html.escape
    bg = (f'background:url({html.escape(image_url, quote=True)}) center/cover, {color};'
          if image_url else f"background:{color};")
    kicker_html = (f'<div class="kick">{esc(kicker or style["kicker"])}</div>')
    # pre-branded source images (photopost) already carry the headline —
    # keep only the CTA layer so the text is never doubled
    plate = (f'<div class="plate">{kicker_html}<h1>{esc(title)}</h1></div>'
             if with_title else "")
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
* {{ margin:0; box-sizing:border-box; font-family:"DejaVu Sans",sans-serif; }}
.story {{ width:1080px; height:1920px; position:relative; overflow:hidden; {bg} }}
.pshade {{ position:absolute; inset:0;
  background:linear-gradient(160deg, rgba(12,6,16,.82) 0%, rgba(12,6,16,.6) 100%); }}
.shade {{ position:absolute; inset:0;
  background:linear-gradient(to top, rgba(8,4,12,.88) 22%, rgba(8,4,12,0) 55%); }}
.brand {{ position:absolute; top:200px; right:48px; background:#fff;
          border-radius:14px; padding:14px 22px; }}
.plate {{ position:absolute; left:0; bottom:520px; max-width:920px;
          background:#fff; padding:52px 60px 52px 56px;
          border-right:20px solid #e3000f;
          box-shadow:0 10px 40px rgba(0,0,0,.3); }}
.kick {{ position:absolute; top:-52px; left:0; background:#e3000f; color:#fff;
         font-weight:bold; font-size:30px; letter-spacing:.1em; padding:10px 22px; }}
.plate h1 {{ font-size:64px; line-height:1.16; font-weight:bold; color:#111; }}
.cta {{ position:absolute; bottom:380px; left:56px; background:#e3000f; color:#fff;
        font-size:40px; font-weight:bold; padding:24px 50px; border-radius:99px; }}
.linkpill {{ position:absolute; bottom:252px; left:56px; background:#fff;
             color:#e3000f; font-size:48px; font-weight:bold;
             padding:22px 52px; border-radius:99px;
             box-shadow:0 8px 30px rgba(0,0,0,.35); }}
.linkpill svg {{ vertical-align:-7px; margin-right:16px; }}
</style></head><body>
<div class="story">
  <div class="shade"></div>
  <div class="brand">{_logo(52)}</div>
  {plate}
  <div class="cta">Lasi visu rakstā</div>
  <div class="linkpill">{_LINK_ICON}tv3.lv</div>
</div>
</body></html>"""


def render_story(title: str, section: str, image_url: str,
                 kicker: str = "", out_dir: Path | None = None,
                 with_title: bool = True) -> str:
    from playwright.sync_api import sync_playwright

    out_dir = Path(out_dir or CARDS_DIR).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(6)
    tmp = out_dir / f"_t{token}.html"
    tmp.write_text(build_story_html(title, section, image_url, kicker,
                                    with_title=with_title),
                   encoding="utf-8")
    chromium = os.environ.get("PLAYWRIGHT_CHROMIUM", "")
    try:
        with sync_playwright() as p:
            browser = (p.chromium.launch(executable_path=chromium) if chromium
                       else p.chromium.launch())
            page = browser.new_page(viewport={"width": 1080, "height": 1920})
            page.goto(tmp.as_uri(), timeout=30000)
            page.wait_for_timeout(800)
            out = out_dir / f"story_{token}.png"
            page.locator(".story").screenshot(path=str(out), timeout=15000)
            browser.close()
    finally:
        tmp.unlink(missing_ok=True)
    return str(out)


def render_cards(title: str, section: str, tag: str, points: list[str],
                 image_url: str, end_question: str,
                 out_dir: Path | None = None,
                 cover_title: bool = True, point_bg: str = "") -> list[str]:
    """Render carousel cards to PNG files; returns local file paths."""
    from playwright.sync_api import sync_playwright

    out_dir = Path(out_dir or CARDS_DIR).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    html_doc = build_cards_html(title, section, tag, points, image_url,
                                end_question, cover_title=cover_title,
                                point_bg=point_bg)
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
