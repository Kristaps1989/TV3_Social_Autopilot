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

# Grafiku izkārtojuma versija. Attēlus renderējam LĒMUMA brīdī un glabājam uz
# ieraksta, tāpēc ieraksts, kas jau stāv rindā, citādi izietu ēterā ar veco
# dizainu vēl ilgi pēc labojuma. Paceļot šo skaitli, publicēšanas solis
# pārrenderē katru rindā gaidošo foto/stāsta grafiku (app.pipeline
# .refresh_missing_media). Paceļ to KATRU reizi, kad mainās izkārtojums.
RENDER_VERSION = 2

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


def date_chip(date_txt: str = "") -> str:
    """Small tv3.lv + date stamp burned into every rendered graphic, so an
    old post resurfacing in the feed can never mislead about when it is
    from. Numeric date — no grammar to get wrong."""
    if not date_txt:
        date_txt = datetime.utcnow().strftime("%d.%m.%Y")
    return f'<div class="dchip">© tv3.lv · {html.escape(date_txt)}</div>'


DCHIP_CSS = (".dchip { position:absolute; left:24px; top:24px; z-index:5;"
             " background:rgba(12,10,14,.62); color:#fff; font-size:22px;"
             " letter-spacing:.04em; padding:8px 18px; border-radius:99px; }")


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


def fit_size(text: str, base: int) -> int:
    """Fonta izmērs, kas garam tekstam neļauj iziet ārpus kartītes. Kartīte ir
    fiksēta (1080×940 attēla daļa) un grieztu pārpalikumu nost — labāk mazāki
    burti nekā vārds uz pusēm."""
    n = len(text or "")
    for limit, scale in ((60, 1.0), (90, .88), (120, .78), (160, .68)):
        if n <= limit:
            return round(base * scale)
    return round(base * .6)


def _settle(page, ms: int = 600) -> None:
    """Ļauj attēliem ienākt, pirms taisām ekrānuzņēmumu. Fiksēts miegs bija
    par īsu lēnam CDN — tad kartīte iznāca kā tukšs krāsas laukums; tagad
    gaidām, līdz tīkls norimst, un tikai pēc tam fiksēto brīdi."""
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:  # noqa: BLE001 — lēns attēls nav iemesls nerenderēt
        pass
    page.wait_for_timeout(ms)


def build_cards_html(title: str, section: str, tag: str, points: list[str],
                     image_url: str, end_question: str,
                     show_sponsor: bool | None = None,
                     cover_title: bool = True, point_bg: str = "",
                     date_txt: str = "", point_images: list[str] | None = None,
                     point_blur: list[str] | None = None,
                     cover_blur: str = "",
                     point_dates: list[str] | None = None,
                     include_cover: bool = True, include_end: bool = True,
                     label: str = "", ai_note: bool = False) -> str:
    """cover_title=False: the cover image is a pre-branded graphic that
    already carries the headline — show it full-bleed without our plate.
    point_bg: photo used as a dimmed background on the content cards so the
    gallery is visual, not flat color blocks.

    point_images: pa foto KATRAI kartītei (digest sarakstiem) — katrs stāsts
    ar savu bildi. Tukša virkne krīt atpakaļ uz point_blur, tad uz sadaļas
    gradientu, nekad uz svešu attēlu.
    point_blur / cover_blur: raksta paša attēls, ko drīkst likt TIKAI stipri
    izpludinātu un aptumšotu — tā ir photopost grafika ar iestrādātu
    virsrakstu, un daudziem tv3.lv rakstiem cita attēla nav. Izpludināts tas
    kļūst par krāsu faktūru: teksts vairs nav salasāms un nedublējas, bet
    kartīte vairs nav plakans krāsas laukums.
    include_cover / include_end: Facebook karuselī ir tikai 5 kartītes, tāpēc
    «TOP 5» saraksts izlaiž vāka un CTA kartīti — piecas kartītes = pieci
    stāsti, katrs ar savu saiti. Ievadu nes paša ieraksta teksts.
    label: franšīzes lente uz pirmās kartītes, kad vāka kartītes nav."""
    from app import disclosure

    style = SECTION_STYLE.get(section) or SECTION_STYLE["news"]
    color = style["color"]
    dark = _shade(color, -0.18)
    esc = html.escape

    def attr(url: str) -> str:
        return html.escape(url, quote=True)

    listing = point_images is not None
    imgs = list(point_images or [])
    blurs = list(point_blur or [])
    dates = list(point_dates or [])
    total = len(points) + (1 if include_cover else 0) + (1 if include_end else 0)

    if show_sponsor is None:
        show_sponsor = SHOW_SPONSOR
    swoosh = ""
    if show_sponsor:
        swoosh = ('<svg class="sw" viewBox="0 0 1080 810" preserveAspectRatio="none">'
                  '<path d="M 800,0 C 716,6 688,34 699,82 C 728,188 908,290 1080,332 '
                  'L 1080,0 Z" fill="#fff"/></svg>'
                  '<div class="sad">SADARBĪBĀ AR</div>')

    def bar(page: int) -> str:
        cells = "".join(f'<i class="{"on" if i <= page else ""}"></i>'
                        for i in range(1, total + 1))
        step = (f'<div class="step">{page}/{total}'
                f'{"<b>&rsaquo;</b>" if page < total else ""}</div>')
        return f"""<div class="bar">
          <div class="prog">{cells}</div>
          {_logo(88)}{step}
          <div class="lbl"><div class="sec"><span>/</span>{esc(style['label'])}</div>
          <div class="tag">{esc(tag)}</div></div></div>"""

    cards = []
    if include_cover:
        cover_blur_layer = ""
        if image_url:
            cover_bg = f'background:url({attr(image_url)}) center/cover, {color};'
        elif cover_blur:
            cover_bg = (f"background:linear-gradient(160deg,"
                        f"{_shade(color, .06)},{_shade(color, -.2)});")
            cover_blur_layer = (f'<div class="blurbg" style="background-image:'
                                f'url({attr(cover_blur)})"></div>')
        else:
            cover_bg = f"background:{color};"
        # the darkening gradient exists to keep the headline readable over a
        # photo; on a flat color card it just muddies the brand color
        shade = '<div class="shade"></div>' if image_url and cover_title else ""
        cover_txt = (f"""<div class="cover-txt">
          <div class="kicker" style="background:{dark}">{esc(style['kicker'])}</div>
          <h1>{esc(title)}</h1>
        </div>""" if cover_title else "")
        cards.append(f"""
    <div class="card">
      <div class="art" style="{cover_bg}">
        {cover_blur_layer}{shade}{swoosh}
        {date_chip(date_txt)}
        <div class="page">1/{total} →</div>
        {cover_txt}
        {disclosure.badge_html() if ai_note else ""}
      </div>{bar(1)}
    </div>""")

    for n, point in enumerate(points, start=1):
        pos = len(cards) + 1
        photo = imgs[n - 1] if n - 1 < len(imgs) else ""
        blur = blurs[n - 1] if n - 1 < len(blurs) else ""
        blur_layer = ""
        if listing:
            # saraksta kartīte: teksts apakšā uz foto, kā izdevēju listiklos
            point_cls = "point low"
            num_color = "rgba(255,255,255,.55)"
            if photo:
                art_style = f'background:url({attr(photo)}) center/cover, {color};'
                point_shade = '<div class="gshade"></div>'
            elif blur:   # tikai photopost grafika — der vienīgi kā faktūra
                art_style = (f"background:linear-gradient(160deg,"
                             f"{_shade(color, .06)},{_shade(color, -.2)});")
                blur_layer = (f'<div class="blurbg" style="background-image:'
                              f'url({attr(blur)})"></div>')
                point_shade = '<div class="gshade"></div>'
            else:
                art_style = (f"background:linear-gradient(160deg,"
                             f"{_shade(color, .06)},{_shade(color, -.2)});")
                point_shade = ""
        elif point_bg:
            point_cls = "point"
            num_color = "rgba(255,255,255,.45)"
            art_style = f'background:url({attr(point_bg)}) center/cover, {color};'
            point_shade = '<div class="pshade"></div>'
        else:
            # bez foto: sadaļas gradients, nevis plakans krāsas laukums —
            # vienlīdz drošs kvīza jautājumam (foto nodotu atbildi), bet
            # plūsmā izskatās kā noformēta kartīte, ne kā tukšums
            point_cls = "point"
            num_color = "rgba(255,255,255,.4)"
            art_style = (f"background:linear-gradient(160deg,"
                         f"{_shade(color, .06)},{_shade(color, -.2)});")
            point_shade = ""
        ribbon = (f'<div class="ribbon" style="background:{dark}">{esc(label)}</div>'
                  if label and pos == 1 else "")
        # saraksta kartītē datums ir sava maza rinda blakus numuram — iekavās
        # aiz virsraksta tas lauza rindu un izskatījās pēc atrunas
        point_date = dates[n - 1] if n - 1 < len(dates) else ""
        num_html = f'<div class="num" style="color:{num_color}">{n}</div>'
        head = (f'<div class="meta">{num_html}'
                f'<span class="pdate">{esc(point_date)}</span></div>'
                if listing and point_date else num_html)
        cards.append(f"""
    <div class="card">
      <div class="art" style="{art_style}">
        {blur_layer}{point_shade}{swoosh}
        {date_chip(date_txt)}
        <div class="page">{pos}/{total} →</div>
        {ribbon}
        <div class="{point_cls}">
          {head}
          <p style="font-size:{fit_size(point, 50 if listing else 54)}px">
            {esc(point)}</p>
        </div>
        <div class="dots">{''.join('<i class="on"></i>' if i == pos - 1 else '<i></i>'
                                    for i in range(total))}</div>
      </div>{bar(pos)}
    </div>""")

    if include_end:
        cards.append(f"""
    <div class="card">
      <div class="art end" style="background:{color}">
        <div class="endlogo">{_logo(150)}</div>
        <h2>{esc(end_question)}</h2>
        <div class="cta" style="color:{color}">Lasi pilno rakstu →</div>
        <div class="url">tv3.lv</div>
        {f'<div class="ainote"><b>MI</b>{esc(disclosure.text())}</div>'
         if ai_note and disclosure.text() else ""}
      </div>
    </div>""")

    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
* {{ margin:0; box-sizing:border-box; font-family:"DejaVu Sans",sans-serif; }}
{DCHIP_CSS}
.dchip {{ left:auto; right:28px; top:28px; }}
.card {{ width:1080px; height:1080px; overflow:hidden; display:flex;
        flex-direction:column; background:#fff; }}
.art {{ position:relative; height:940px; overflow:hidden; flex:none; }}
.pshade {{ position:absolute; inset:0;
  background:linear-gradient(160deg, rgba(12,6,16,.82) 0%, rgba(12,6,16,.6) 100%); }}
.blurbg {{ position:absolute; inset:-60px; background-size:cover;
  background-position:center; filter:blur(30px) brightness(.78) saturate(1.15); }}
.gshade {{ position:absolute; inset:0;
  background:linear-gradient(to top, rgba(10,5,15,.95) 24%,
    rgba(10,5,15,.3) 64%, rgba(10,5,15,.5) 100%); }}
.shade {{ position:absolute; inset:0;
  background:linear-gradient(to top, rgba(10,5,15,.92) 18%, rgba(10,5,15,.1) 55%); }}
.sw {{ position:absolute; top:0; right:0; width:100%; height:100%; }}
.sad {{ position:absolute; top:44px; right:40px; color:#111; font-size:26px;
        letter-spacing:.06em; }}
.page {{ position:absolute; top:44px; left:48px; color:#fff; font-size:30px;
         opacity:.9; font-weight:bold; }}
.ribbon {{ position:absolute; top:104px; left:48px; color:#fff;
           font-weight:bold; font-size:30px; letter-spacing:.12em;
           padding:12px 26px; border-radius:8px; }}
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
.point.low {{ justify-content:flex-end; padding:0 78px 104px; }}
.meta {{ display:flex; align-items:baseline; gap:26px; margin-bottom:6px; }}
.pdate {{ font-size:30px; font-weight:bold; letter-spacing:.16em;
          text-transform:uppercase; color:rgba(255,255,255,.75); }}
.point.low .num {{ font-size:96px; margin-bottom:0; }}
.point.low p {{ font-size:50px; line-height:1.24;
                text-shadow:0 4px 24px rgba(0,0,0,.55); }}
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
.ainote {{ position:absolute; left:64px; right:64px; bottom:56px;
  display:flex; align-items:center; justify-content:center; gap:16px;
  font-size:27px; font-weight:600; line-height:1.3; opacity:.95;
  border-top:2px solid rgba(255,255,255,.38); padding-top:24px; }}
.ainote b {{ background:#fff; color:{color}; border-radius:8px;
  padding:3px 11px; font-size:26px; flex:none; }}
{disclosure.badge_css(left=48, bottom=44, size=26)}
.aibadge {{ z-index:3; }}
</style></head><body>{''.join(cards)}</body></html>"""


_CHEVRONS = ('<svg width="120" height="64" viewBox="0 0 120 64">'
             '<g fill="none" stroke="#e3000f" stroke-width="14" '
             'stroke-linecap="round" stroke-linejoin="round">'
             '<path d="M12 8 L36 32 L12 56"/><path d="M48 8 L72 32 L48 56"/>'
             '<path d="M84 8 L108 32 L84 56"/></g></svg>')


def body_fit(text: str, base: int = 40) -> int:
    """Sadaļas pamatteksta izmērs: 2-4 teikumi ir garāki par punktu, tāpēc
    sava skala — kartīte ir fiksēta, un teksts nedrīkst iziet no paneļa."""
    n = len(text or "")
    for limit, scale in ((140, 1.0), (200, .9), (260, .82), (320, .74)):
        if n <= limit:
            return round(base * scale)
    return round(base * .66)


def build_section_cards_html(title: str, section: str, tag: str,
                             sections: list[dict], images: list[str],
                             end_question: str, cover_image: str = "",
                             cover_title: bool = True, blur_image: str = "",
                             date_txt: str = "", ai_note: bool = False) -> str:
    """Karuselis, kur katra kartīte ir stāsta SADAĻA: trekns virsraksts un
    2-4 teikumi ar faktiem — nevis viens punkts lielā fontā.

    Paraugs ir tas, ko dara labākie ziņu konti: pilns foto fonā (katrai
    kartītei savs, no raksta galerijas) un pa vidu puscaurspīdīgs balts
    panelis ar virsrakstu un tekstu. Vāks un CTA beigu kartīte paliek mūsu
    ierastajā stilā.

    Švīkošanas norāde ir baltajā apakšjoslā, nevis uz foto: sarkana nodaļu
    josla joslas augšmalā un «N/M →» zīmīte pie logo. Uz attēla stāvējušās
    ">>>" bultas gulās virsū virsrakstam un bija cita medija zīme; josla ir
    tā pati, kas lentes kadros, tāpēc formāti izskatās kā viena franšīze.

    images: foto sadaļu kartītēm, tiek ciklēti pēc kārtas. Ja neviena tīra
    foto nav (tikai photopost grafika), blur_image kļūst par izpludinātu
    faktūru — tāpat kā pārējos formātos.
    """
    from app import disclosure

    style = SECTION_STYLE.get(section) or SECTION_STYLE["news"]
    color = style["color"]
    dark = _shade(color, -0.18)
    esc = html.escape

    def attr(url: str) -> str:
        return html.escape(url, quote=True)

    total = len(sections) + 2      # vāks + sadaļas + CTA
    pool = [i for i in images if i]

    def bar(page: int) -> str:
        cells = "".join(f'<i class="{"on" if i <= page else ""}"></i>'
                        for i in range(1, total + 1))
        step = (f'<div class="step">{page}/{total}'
                f'{"<b>&rsaquo;</b>" if page < total else ""}</div>')
        return f"""<div class="bar">
          <div class="prog">{cells}</div>
          {_logo(88)}{step}
          <div class="lbl"><div class="sec"><span>/</span>{esc(style['label'])}</div>
          <div class="tag">{esc(tag)}</div></div></div>"""

    cards = []
    # --- vāks
    gradient = (f"background:linear-gradient(160deg,"
                f"{_shade(color, .06)},{_shade(color, -.2)});")
    whole = ""
    if cover_image and not cover_title:
        # Gatava photopost grafika: tās pašas izkārtojums IR vāks — virsraksta
        # plāksne, atkāpes, sarkanā svītra. `cover` to iegriež kartes rāmī,
        # un plāksne tad tiek nogriezta pusvārdā un uzguļas mūsu baltajai
        # apakšjoslai. Tāpēc rādām grafiku VESELU uz tās pašas izpludinātās
        # kopijas — tāpat, kā to jau dara vertikālais stāsts.
        cover_bg = gradient
        blur_layer = (f'<div class="blurbg" style="background-image:'
                      f'url({attr(cover_image)})"></div>')
        whole = f'<img class="whole" src="{attr(cover_image)}">'
    elif cover_image:
        cover_bg = f'background:url({attr(cover_image)}) center/cover, {color};'
        blur_layer = ""
    elif blur_image:
        cover_bg = gradient
        blur_layer = (f'<div class="blurbg" style="background-image:'
                      f'url({attr(blur_image)})"></div>')
    else:
        cover_bg = f"background:{color};"
        blur_layer = ""
    shade = '<div class="shade"></div>' if cover_image and cover_title else ""
    cover_txt = (f"""<div class="cover-txt">
      <div class="kicker" style="background:{dark}">{esc(style['kicker'])}</div>
      <h1 style="font-size:{fit_size(title, 66)}px">{esc(title)}</h1>
    </div>""" if cover_title else "")
    cards.append(f"""
    <div class="card">
      <div class="art" style="{cover_bg}">
        {blur_layer}{whole}{shade}
        {date_chip(date_txt)}
        {cover_txt}
        {disclosure.badge_html() if ai_note else ""}
      </div>{bar(1)}
    </div>""")

    # --- sadaļu kartītes: foto + balts panelis + virsraksts + teksts
    for n, sec in enumerate(sections, start=1):
        pos = n + 1
        head = str(sec.get("title") or "").strip()
        body = str(sec.get("body") or "").strip()
        photo = pool[(n - 1) % len(pool)] if pool else ""
        if photo:
            art_style = f'background:url({attr(photo)}) center/cover, {color};'
            blur_layer = ""
        elif blur_image:
            art_style = (f"background:linear-gradient(160deg,"
                         f"{_shade(color, .06)},{_shade(color, -.2)});")
            blur_layer = (f'<div class="blurbg" style="background-image:'
                          f'url({attr(blur_image)})"></div>')
        else:
            art_style = (f"background:linear-gradient(160deg,"
                         f"{_shade(color, .06)},{_shade(color, -.2)});")
            blur_layer = ""
        cards.append(f"""
    <div class="card">
      <div class="art" style="{art_style}">
        {blur_layer}<div class="veil"></div>
        {date_chip(date_txt)}
        <div class="panelwrap"><div class="panel">
          <h3 style="font-size:{fit_size(head, 52)}px">{esc(head)}</h3>
          <p style="font-size:{body_fit(body)}px">{esc(body)}</p>
        </div></div>
      </div>{bar(pos)}
    </div>""")

    # --- CTA beigu kartīte: kā līdz šim
    cards.append(f"""
    <div class="card">
      <div class="art end" style="background:{color}">
        <div class="endlogo">{_logo(150)}</div>
        <h2>{esc(end_question)}</h2>
        <div class="cta" style="color:{color}">Lasi pilno rakstu →</div>
        <div class="url">tv3.lv</div>
        {f'<div class="ainote"><b>MI</b>{esc(disclosure.text())}</div>'
         if ai_note and disclosure.text() else ""}
      </div>
    </div>""")

    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
* {{ margin:0; box-sizing:border-box; font-family:"DejaVu Sans",sans-serif; }}
{DCHIP_CSS}
.dchip {{ left:auto; right:28px; top:28px; }}
.card {{ width:1080px; height:1080px; overflow:hidden; display:flex;
        flex-direction:column; background:#fff; }}
.art {{ position:relative; height:940px; overflow:hidden; flex:none; }}
.blurbg {{ position:absolute; inset:-60px; background-size:cover;
  background-position:center; filter:blur(30px) brightness(.78) saturate(1.15); }}
/* Gatavā grafika redzama VESELA. Atkāpe no apakšas ir apzināta: tās pašas
   virsraksta plāksne citādi pieskaras mūsu baltajai joslai, un divi balti
   saplūst vienā laukumā, kurā virsraksts izskatās iesprūdis. */
.whole {{ position:absolute; top:20px; left:0; right:0; bottom:36px;
  width:100%; height:calc(100% - 56px); object-fit:contain;
  filter:drop-shadow(0 10px 34px rgba(0,0,0,.45)); }}
.veil {{ position:absolute; inset:0; background:rgba(10,8,14,.28); }}
.shade {{ position:absolute; inset:0;
  background:linear-gradient(to top, rgba(10,5,15,.92) 18%, rgba(10,5,15,.1) 55%); }}
.cover-txt {{ position:absolute; bottom:0; left:0; right:0;
              padding:56px 64px 76px 56px; color:#fff; }}
.kicker {{ display:inline-block; color:#fff; font-weight:bold; font-size:28px;
           letter-spacing:.12em; padding:10px 24px; border-radius:8px;
           margin-bottom:26px; }}
h1 {{ line-height:1.14; font-weight:bold; }}
.panelwrap {{ position:absolute; inset:0; display:flex; align-items:center;
              justify-content:center; padding:0 76px; }}
/* Panelis ir jālasa arī uz spilgta, raiba foto. Necaurspīdība viena pati
   to nedod: caur 90% spilgtas krāsas joprojām spiežas cauri un panelis
   izskatās notraipīts. backdrop-filter to, kas aiz muguras, pārvērš par
   faktūru — svītras kļūst par vienmērīgu fonu, teksts paliek melns uz balta. */
.panel {{ background:rgba(255,255,255,.96);
          backdrop-filter:blur(16px) saturate(.75);
          -webkit-backdrop-filter:blur(16px) saturate(.75);
          border-radius:20px;
          padding:56px 60px; max-width:880px; text-align:center;
          box-shadow:0 16px 60px rgba(0,0,0,.32); }}
.panel h3 {{ color:#111; line-height:1.18; font-weight:bold;
             margin-bottom:30px; }}
.panel p {{ color:#20242c; line-height:1.42; font-weight:600; }}
/* Švīkošanas norāde dzīvo baltajā joslā, ne uz foto: sarkanā nodaļu josla
   pa visu platumu un «N/M ›» pie logo. Uz attēla stāvējušās ">>>" bultas
   gulās virsū virsrakstam un bija skaidri cita medija zīme; šī josla ir tā
   pati, kas lentes kadros, tāpēc formāti izskatās kā viena franšīze. */
.bar {{ position:relative; height:140px; background:#fff; display:flex;
        align-items:center; justify-content:space-between; padding:0 48px;
        flex:none; }}
/* Sliede ir TUMŠA ar nolūku: tā vienlaikus rāda progresu un novelk skaidru
   robežu starp attēlu un balto joslu. Gaiši pelēkas nepildītās daļas ar
   balto joslu saplūda vienā laukumā. */
.prog {{ position:absolute; top:0; left:0; right:0; height:11px;
         display:flex; gap:4px; background:#1c1f24; }}
.prog i {{ flex:1; background:transparent; }}
.prog i.on {{ background:#e3000f; }}
.step {{ display:flex; align-items:center; gap:14px; color:#6b7280;
         font-weight:800; font-size:32px; letter-spacing:.02em; }}
.step b {{ color:#e3000f; font-size:52px; line-height:.8; }}
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
.ainote {{ position:absolute; left:64px; right:64px; bottom:56px;
  display:flex; align-items:center; justify-content:center; gap:16px;
  font-size:27px; font-weight:600; line-height:1.3; opacity:.95;
  border-top:2px solid rgba(255,255,255,.38); padding-top:24px; }}
.ainote b {{ background:#fff; color:{color}; border-radius:8px;
  padding:3px 11px; font-size:26px; flex:none; }}
{disclosure.badge_css(left=48, bottom=44, size=26)}
.aibadge {{ z-index:3; }}
</style></head><body>{''.join(cards)}</body></html>"""


def build_share_html(title: str, section: str, image_url: str,
                     kicker: str = "", blur_image: str = "", width: int = 1080,
                     height: int = 1080, date_txt: str = "") -> str:
    """Single branded share image: article photo full-bleed with the tv3.lv
    title plate — white box, bold headline, red accent bar — matching the
    tv3.lv site's own share-image style. Size per platform: FB feed 4:5
    (1080x1350, max feed real estate), X/Threads 1:1 (1080x1080, no crop)."""
    style = SECTION_STYLE.get(section) or SECTION_STYLE["news"]
    color = style["color"]
    esc = html.escape
    blur_layer = ""
    if image_url:
        bg = (f'background:url({html.escape(image_url, quote=True)}) '
              f'center/cover, {color};')
    else:
        bg = f"background:{color};"
        if blur_image:
            blur_layer = (f'<div class="blurbg" style="background-image:'
                          f'url({html.escape(blur_image, quote=True)})"></div>')
    kicker_html = (f'<div class="kick">{esc(kicker)}</div>' if kicker else "")
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
* {{ margin:0; box-sizing:border-box; font-family:"DejaVu Sans",sans-serif; }}
.share {{ width:{width}px; height:{height}px; position:relative; overflow:hidden; {bg} }}
.blurbg {{ position:absolute; inset:-60px; background-size:cover;
  background-position:center; filter:blur(30px) brightness(.78) saturate(1.15); }}
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
{DCHIP_CSS}
</style></head><body>
<div class="share">
  {blur_layer}
  {date_chip(date_txt)}
  <div class="plate">{kicker_html}<h1>{esc(title)}</h1></div>
  <div class="brand">{_logo(44)}</div>
</div>
</body></html>"""


def render_share_image(title: str, section: str, image_url: str,
                       kicker: str = "", out_dir: Path | None = None,
                       width: int = 1080, height: int = 1080,
                       date_txt: str = "", blur_image: str = "") -> str:
    from playwright.sync_api import sync_playwright

    out_dir = Path(out_dir or CARDS_DIR).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(6)
    tmp = out_dir / f"_s{token}.html"
    # ar atslēgvārdiem: blur_image ielikšana starp kicker un width pozicionālā
    # izsaukumā klusi pārvērstu platumu par attēla adresi
    tmp.write_text(build_share_html(title, section, image_url, kicker=kicker,
                                    blur_image=blur_image, width=width,
                                    height=height, date_txt=date_txt),
                   encoding="utf-8")
    chromium = os.environ.get("PLAYWRIGHT_CHROMIUM", "")
    try:
        with sync_playwright() as p:
            browser = (p.chromium.launch(executable_path=chromium) if chromium
                       else p.chromium.launch())
            page = browser.new_page(viewport={"width": width, "height": height})
            page.goto(tmp.as_uri(), timeout=30000)
            _settle(page, 800)
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
                     kicker: str = "", with_title: bool = True,
                     date_txt: str = "", inset: int = 0,
                     blur_image: str = "", ai_badge: bool = False) -> str:
    """Vertical story (1080x1920) in the tv3.lv style: full-bleed image,
    title plate, CTA. Top/bottom safe zones respected (platform UI).

    inset: cik pikseļu no SĀNU malām atkāpties. Statiskā stāstā tas ir 0 un
    virsraksta plāksne aiziet līdz pašai malai. Lentē kadru tuvina Ken Burns
    efekts, un tuvinājums apgriež tieši malas — tur plāksne pazūd pa vidu
    vārdam, tāpēc reels padod savu SAFE_INSET.

    blur_image: ko likt fonā, kad tīra foto NAV. Photopost grafika ar iecepto
    virsrakstu zem mūsu plāksnes neder, bet izpludināta tā ir laba faktūra —
    daudz labāka par plakanu krāsas laukumu, kāds vāks iznāca līdz šim.

    ai_badge: ES MI akta marķējums (Regula 2024/1689, 50. pants). Statiskam
    stāstam to uzliek zvanītājs; lentē tas ir vienmēr.
    """
    from app import disclosure
    style = SECTION_STYLE.get(section) or SECTION_STYLE["news"]
    color = style["color"]
    esc = html.escape
    img = html.escape(image_url, quote=True) if image_url else ""
    kicker_html = (f'<div class="kick">{esc(kicker or style["kicker"])}</div>')
    if with_title:
        # our own layout: full-bleed photo, gradient, white title plate
        blur = html.escape(blur_image, quote=True) if blur_image else ""
        if img:
            bg = f'background:url({img}) center/cover, {color};'
            layers = '<div class="shade"></div>'
        elif blur:
            bg = f"background:{color};"
            layers = (f'<div class="bgblur" style="background-image:url({blur})">'
                      f'</div><div class="shade"></div>')
        else:
            bg = f"background:{color};"
            layers = '<div class="shade"></div>'
        plate = f'<div class="plate">{kicker_html}<h1>{esc(title)}</h1></div>'
    else:
        # pre-branded graphic (photopost): its own headline IS the layout, so
        # it must stay fully visible — center/cover would crop the text on a
        # 9:16 canvas. Show the whole graphic (contain) over a blurred copy,
        # and keep only the CTA layer.
        bg = f"background:{color};"
        layers = (f'<div class="bgblur"></div><img class="art" src="{img}">'
                  if img else "")
        plate = ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
* {{ margin:0; box-sizing:border-box; font-family:"DejaVu Sans",sans-serif; }}
.story {{ width:1080px; height:1920px; position:relative; overflow:hidden; {bg} }}
.bgblur {{ position:absolute; inset:-60px;
  background:url({img}) center/cover, {color};
  background-size:cover; background-position:center;
  filter:blur(48px) brightness(.55); }}
{disclosure.badge_css(left=56 + inset, bottom=170, size=28) if ai_badge else ""}
.art {{ position:absolute; top:190px; left:50%; transform:translateX(-50%);
  max-width:1016px; max-height:1230px; object-fit:contain;
  border-radius:22px; box-shadow:0 18px 60px rgba(0,0,0,.45); }}
{DCHIP_CSS}
.dchip {{ left:auto; top:auto; right:{48 + inset}px; bottom:160px; }}
.pshade {{ position:absolute; inset:0;
  background:linear-gradient(160deg, rgba(12,6,16,.82) 0%, rgba(12,6,16,.6) 100%); }}
.shade {{ position:absolute; inset:0;
  background:linear-gradient(to top, rgba(8,4,12,.88) 22%, rgba(8,4,12,0) 55%); }}
.brand {{ position:absolute; top:200px; right:{48 + inset}px; background:#fff;
          border-radius:14px; padding:14px 22px; }}
.plate {{ position:absolute; left:{inset}px; bottom:520px; max-width:{920 - inset}px;
          background:#fff; padding:52px 60px 52px 56px;
          border-right:20px solid #e3000f;
          box-shadow:0 10px 40px rgba(0,0,0,.3); }}
.kick {{ position:absolute; top:-52px; left:0; background:#e3000f; color:#fff;
         font-weight:bold; font-size:30px; letter-spacing:.1em; padding:10px 22px; }}
.plate h1 {{ font-size:64px; line-height:1.16; font-weight:bold; color:#111; }}
.cta {{ position:absolute; bottom:380px; left:{56 + inset}px; background:#e3000f;
        color:#fff; font-size:40px; font-weight:bold; padding:24px 50px;
        border-radius:99px; }}
.linkpill {{ position:absolute; bottom:252px; left:{56 + inset}px; background:#fff;
             color:#e3000f; font-size:48px; font-weight:bold;
             padding:22px 52px; border-radius:99px;
             box-shadow:0 8px 30px rgba(0,0,0,.35); }}
.linkpill svg {{ vertical-align:-7px; margin-right:16px; }}
</style></head><body>
<div class="story">
  {layers}
  {date_chip(date_txt)}
  <div class="brand">{_logo(52)}</div>
  {plate}
  <div class="cta">Lasi visu rakstā</div>
  <div class="linkpill">{_LINK_ICON}tv3.lv</div>
  {disclosure.badge_html() if ai_badge else ""}
</div>
</body></html>"""


def build_mosaic_story_html(title: str, section: str, images: list[str],
                            date_txt: str = "") -> str:
    """9:16 vāks no vairākiem rakstu foto — 2×3 mozaīka ar tumšu pārklājumu
    un virsraksta plāksni. Nedēļas digest vākam: viens attēls nepasaka
    "nedēļa", mozaīka pasaka."""
    style = SECTION_STYLE.get(section) or SECTION_STYLE["news"]
    color = style["color"]
    esc = html.escape
    pics = [i for i in images if i][:6]
    while pics and len(pics) < 6:
        pics.append(pics[len(pics) % max(1, len(images))])
    cells = "".join(
        f'<div class="cell" style="background:url({html.escape(u, quote=True)}) '
        f'center/cover, {color}"></div>' for u in pics)
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
* {{ margin:0; box-sizing:border-box; font-family:"DejaVu Sans",sans-serif; }}
.story {{ width:1080px; height:1920px; position:relative; overflow:hidden;
  background:{color}; }}
.grid {{ position:absolute; inset:0; display:grid; gap:6px;
  grid-template-columns:1fr 1fr; grid-template-rows:1fr 1fr 1fr; }}
.cell {{ width:100%; height:100%; }}
.shade {{ position:absolute; inset:0;
  background:linear-gradient(to top, rgba(8,4,12,.9) 20%, rgba(8,4,12,.25) 60%); }}
.brand {{ position:absolute; top:200px; right:48px; background:#fff;
          border-radius:14px; padding:14px 22px; }}
.plate {{ position:absolute; left:0; bottom:430px; max-width:920px;
          background:#fff; padding:52px 60px 52px 56px;
          border-right:20px solid #e3000f;
          box-shadow:0 10px 40px rgba(0,0,0,.3); }}
.kick {{ position:absolute; top:-52px; left:0; background:#e3000f; color:#fff;
         font-weight:bold; font-size:30px; letter-spacing:.1em; padding:10px 22px; }}
.plate h1 {{ font-size:72px; line-height:1.14; font-weight:bold; color:#111; }}
{DCHIP_CSS}
.dchip {{ left:auto; top:auto; right:48px; bottom:160px; }}
</style></head><body>
<div class="story">
  <div class="grid">{cells}</div>
  <div class="shade"></div>
  {date_chip(date_txt)}
  <div class="brand">{_logo(52)}</div>
  <div class="plate"><div class="kick">{esc(style['kicker'])}</div>
    <h1>{esc(title)}</h1></div>
</div>
</body></html>"""


def render_mosaic_story(title: str, section: str, images: list[str],
                        date_txt: str = "",
                        out_dir: Path | None = None) -> str:
    from playwright.sync_api import sync_playwright

    out_dir = Path(out_dir or CARDS_DIR).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(6)
    tmp = out_dir / f"_m{token}.html"
    tmp.write_text(build_mosaic_story_html(title, section, images,
                                           date_txt=date_txt),
                   encoding="utf-8")
    chromium = os.environ.get("PLAYWRIGHT_CHROMIUM", "")
    try:
        with sync_playwright() as pw:
            browser = (pw.chromium.launch(executable_path=chromium) if chromium
                       else pw.chromium.launch())
            page = browser.new_page(viewport={"width": 1080, "height": 1920})
            page.goto(tmp.as_uri(), timeout=30000)
            _settle(page, 800)
            out = out_dir / f"mosaic_{token}.png"
            page.locator(".story").screenshot(path=str(out), timeout=15000)
            browser.close()
    finally:
        tmp.unlink(missing_ok=True)
    return str(out)


def render_story(title: str, section: str, image_url: str,
                 kicker: str = "", out_dir: Path | None = None,
                 with_title: bool = True, date_txt: str = "") -> str:
    from playwright.sync_api import sync_playwright

    out_dir = Path(out_dir or CARDS_DIR).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(6)
    tmp = out_dir / f"_t{token}.html"
    tmp.write_text(build_story_html(title, section, image_url, kicker,
                                    with_title=with_title, date_txt=date_txt),
                   encoding="utf-8")
    chromium = os.environ.get("PLAYWRIGHT_CHROMIUM", "")
    try:
        with sync_playwright() as p:
            browser = (p.chromium.launch(executable_path=chromium) if chromium
                       else p.chromium.launch())
            page = browser.new_page(viewport={"width": 1080, "height": 1920})
            page.goto(tmp.as_uri(), timeout=30000)
            _settle(page, 800)
            out = out_dir / f"story_{token}.png"
            page.locator(".story").screenshot(path=str(out), timeout=15000)
            browser.close()
    finally:
        tmp.unlink(missing_ok=True)
    return str(out)


def render_cards(title: str, section: str, tag: str, points: list[str],
                 image_url: str, end_question: str,
                 out_dir: Path | None = None,
                 cover_title: bool = True, point_bg: str = "",
                 date_txt: str = "", point_images: list[str] | None = None,
                 point_blur: list[str] | None = None, cover_blur: str = "",
                 point_dates: list[str] | None = None,
                 include_cover: bool = True, include_end: bool = True,
                 label: str = "") -> list[str]:
    """Render carousel cards to PNG files; returns local file paths."""
    from playwright.sync_api import sync_playwright

    out_dir = Path(out_dir or CARDS_DIR).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    html_doc = build_cards_html(title, section, tag, points, image_url,
                                end_question, cover_title=cover_title,
                                point_bg=point_bg, date_txt=date_txt,
                                point_images=point_images,
                                point_blur=point_blur, cover_blur=cover_blur,
                                point_dates=point_dates,
                                include_cover=include_cover,
                                include_end=include_end, label=label)
    return _screenshot_cards(html_doc, out_dir)


def _screenshot_cards(html_doc: str, out_dir: Path) -> list[str]:
    """Katrs .card elements -> savs PNG fails."""
    from playwright.sync_api import sync_playwright

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
            _settle(page)
            for i, el in enumerate(page.locator(".card").all()):
                f = out_dir / f"card_{token}_{i}.png"
                el.screenshot(path=str(f), timeout=15000)
                paths.append(str(f))
            browser.close()
    finally:
        tmp.unlink(missing_ok=True)
    return paths


def render_section_cards(title: str, section: str, tag: str,
                         sections: list[dict], images: list[str],
                         end_question: str, cover_image: str = "",
                         cover_title: bool = True, blur_image: str = "",
                         date_txt: str = "", ai_note: bool = False,
                         out_dir: Path | None = None) -> list[str]:
    """Sadaļu karuseļa kartītes kā PNG faili (vāks + sadaļas + CTA)."""
    out_dir = Path(out_dir or CARDS_DIR).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    html_doc = build_section_cards_html(
        title, section, tag, sections, images, end_question,
        cover_image=cover_image, cover_title=cover_title,
        blur_image=blur_image, date_txt=date_txt, ai_note=ai_note)
    return _screenshot_cards(html_doc, out_dir)


def build_number_html(number: str, context: str, section: str,
                      image_url: str = "", date_txt: str = "",
                      width: int = 1080, height: int = 1350,
                      blur_image: str = "") -> str:
    """«Nedēļas skaitlis»: viens liels skaitlis un viena konteksta rinda uz
    aptumšota raksta foto. Pusdienlaika formāts — apstādina ritināšanu bez
    lasīšanas, pilnais stāsts paliek rakstā.

    blur_image: ko likt fonā, kad tīra foto NAV. Photopost grafika ar iecepto
    virsrakstu zem mūsu teksta neder, bet izpludināta tā ir laba faktūra —
    daudziem tv3.lv rakstiem cita attēla vienkārši nav, un plakana krāsas
    karte plūsmā zaudē. Pārējie formāti šo rezervi izmanto jau sen; šis bija
    palicis bez tās, tāpēc iznāca kartes bez neviena attēla.
    """
    style = SECTION_STYLE.get(section) or SECTION_STYLE["news"]
    color = style["color"]
    esc = html.escape
    blur_layer = ""
    if image_url:
        bg = (f'background:url({html.escape(image_url, quote=True)}) '
              f'center/cover, {color};')
    else:
        bg = (f"background:linear-gradient(160deg,{_shade(color, .06)},"
              f"{_shade(color, -.16)});")
        if blur_image:
            blur_layer = (f'<div class="blurbg" style="background-image:'
                          f'url({html.escape(blur_image, quote=True)})"></div>')
    # garš skaitlis («1 240 000») nedrīkst izplūst ārpus kartes
    n = len(number)
    size = 300 if n <= 3 else (230 if n <= 5 else (170 if n <= 8 else 120))
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
* {{ margin:0; box-sizing:border-box; font-family:"DejaVu Sans",sans-serif; }}
.numcard {{ width:{width}px; height:{height}px; position:relative;
  overflow:hidden; {bg} }}
.blurbg {{ position:absolute; inset:-60px; background-size:cover;
  background-position:center; filter:blur(30px) brightness(.78) saturate(1.15); }}
.shade {{ position:absolute; inset:0;
  background:linear-gradient(to top, rgba(8,4,12,.92) 30%, rgba(8,4,12,.55) 100%); }}
.kick {{ position:absolute; left:64px; top:150px; background:#e3000f; color:#fff;
  font-weight:bold; font-size:30px; letter-spacing:.12em; padding:12px 24px; }}
.num {{ position:absolute; left:64px; right:64px; top:260px; color:#fff;
  font-size:{size}px; line-height:1; font-weight:bold; letter-spacing:-.02em;
  text-shadow:0 8px 40px rgba(0,0,0,.45); }}
.rule {{ position:absolute; left:64px; top:{260 + size + 40}px; width:180px;
  height:10px; background:#e3000f; }}
.ctx {{ position:absolute; left:64px; right:64px; bottom:230px; color:#fff;
  font-size:52px; line-height:1.28; font-weight:bold; }}
.brand {{ position:absolute; right:56px; bottom:56px; background:#fff;
  border-radius:12px; padding:12px 20px; }}
{DCHIP_CSS}
</style></head><body>
<div class="numcard">
  {blur_layer}<div class="shade"></div>
  {date_chip(date_txt)}
  <div class="kick">NEDĒĻAS SKAITLIS</div>
  <div class="num">{esc(number)}</div>
  <div class="rule"></div>
  <div class="ctx">{esc(context)}</div>
  <div class="brand">{_logo(44)}</div>
</div>
</body></html>"""


def render_number_card(number: str, context: str, section: str,
                       image_url: str = "", date_txt: str = "",
                       out_dir: Path | None = None,
                       blur_image: str = "") -> str:
    from playwright.sync_api import sync_playwright

    out_dir = Path(out_dir or CARDS_DIR).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(6)
    tmp = out_dir / f"_n{token}.html"
    tmp.write_text(build_number_html(number, context, section, image_url,
                                     date_txt=date_txt, blur_image=blur_image),
                   encoding="utf-8")
    chromium = os.environ.get("PLAYWRIGHT_CHROMIUM", "")
    try:
        with sync_playwright() as pw:
            browser = (pw.chromium.launch(executable_path=chromium) if chromium
                       else pw.chromium.launch())
            page = browser.new_page(viewport={"width": 1080, "height": 1350})
            page.goto(tmp.as_uri(), timeout=30000)
            _settle(page, 800)
            out = out_dir / f"number_{token}.png"
            page.locator(".numcard").screenshot(path=str(out), timeout=15000)
            browser.close()
    finally:
        tmp.unlink(missing_ok=True)
    return str(out)
