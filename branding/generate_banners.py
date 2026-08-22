#!/usr/bin/env python3
"""Generate tv3.lv section banners in the Dzīvesstils design system.

Replicates the designer template: flat muted section color, two-tone icon
cluster bottom-right, white "SADARBĪBĀ AR" swoosh top-right, white bar with
the tv3.lv logo and /SECTION #SUBSECTION label. Renders every subsection in
the four standard sizes (1000x400, 1000x125, 800x500, 800x250) plus a
designer-style preview sheet per subsection.

Usage:  python branding/generate_banners.py [out_dir]
Needs:  playwright + chromium (uses PLAYWRIGHT_CHROMIUM env or default install)

NOTE: type is approximated with DejaVu Sans and the logo is a text
approximation — swap in the brand font + logo SVG before production use.
"""
from __future__ import annotations

import colorsys
import os
import sys
from pathlib import Path

# --- palette helpers -------------------------------------------------------

def shade(hex_color: str, dl: float, ds: float = 0.0) -> str:
    """Lighten/darken (dl) and de/saturate (ds) a hex color in HLS space."""
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    l = min(1, max(0, l + dl))
    s = min(1, max(0, s + ds))
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


# --- icon clusters (flat, two-tone, in the template's corner style) --------
# Each icon set is an SVG fragment drawn in a 400x300 viewBox; classes
# i1 (darker tone) / i2 (lighter tone) are tinted per section.

def _star(cx: float, cy: float, r: float, cls: str) -> str:
    import math

    pts = []
    for k in range(10):
        rad = r if k % 2 == 0 else r * 0.42
        ang = -math.pi / 2 + k * math.pi / 5
        pts.append(f"{cx + rad * math.cos(ang):.1f},{cy + rad * math.sin(ang):.1f}")
    return f'<polygon class="{cls}" points="{" ".join(pts)}"/>'


ICONS = {
    # Filled two-tone glyphs; white detail lines echo the original's cutouts.
    "latvija": f"""
      <path class="i1" d="M150 36 a46 46 0 1 1 0.1 0 z M124 68 l26 84 26 -84 z"/>
      <circle class="w" cx="150" cy="82" r="17"/>
      {_star(268, 74, 40, 'i2')}
      {_star(324, 136, 30, 'i1')}
      {_star(368, 190, 22, 'i2')}
      <g transform="rotate(-6 160 235)">
        <rect class="i1" x="70" y="188" width="180" height="38"/>
        <rect class="w" x="70" y="226" width="180" height="16"/>
        <rect class="i1" x="70" y="242" width="180" height="38"/>
      </g>
    """,
    "arzemes": f"""
      <circle class="i1" cx="130" cy="170" r="92"/>
      <ellipse class="wl" cx="130" cy="170" rx="40" ry="92"/>
      <path class="wl" d="M44 138 h172 M44 202 h172"/>
      <path class="i2" d="M240 60 l140 44 -66 22 -18 60 -26 -50 z"/>
      <path class="w" d="M296 126 l40 -14" style="stroke-width:6"/>
      <rect class="i2" x="290" y="200" width="100" height="72" rx="10" transform="rotate(6 340 236)"/>
      <path class="w" d="M296 208 l44 30 46 -24" fill="none" style="stroke-width:8" transform="rotate(6 340 236)"/>
    """,
    "ekonomika": """
      <rect class="i2" x="60" y="190" width="48" height="90"/>
      <rect class="i1" x="122" y="150" width="48" height="130"/>
      <rect class="i2" x="184" y="105" width="48" height="175"/>
      <path class="i1" d="M60 130 L160 76 L252 44 l-14 42 40 -50 -52 6 z"/>
      <circle class="i1" cx="320" cy="140" r="62"/>
      <path class="w" d="M338 116 a30 30 0 1 0 0 48 M296 132 h34 M296 148 h34" fill="none" style="stroke-width:9"/>
      <circle class="i2" cx="360" cy="240" r="42"/>
      <path class="w" d="M372 224 a20 20 0 1 0 0 32 M344 235 h22 M344 246 h22" fill="none" style="stroke-width:7"/>
    """,
    "kriminalzinas": """
      <path class="i1" d="M96 210 a64 64 0 0 1 128 0 z"/>
      <rect class="i2" x="78" y="210" width="164" height="26" rx="10"/>
      <path class="i2" d="M154 108 h12 v-40 h-12 z M104 126 l8 -8 -26 -28 -8 8 z M216 126 l-8 -8 26 -28 8 8 z"/>
      <path class="i2" d="M300 84 a46 46 0 1 0 0.1 0 z"/>
      <circle class="w" cx="300" cy="130" r="26"/>
      <path class="i2" d="M368 116 a46 46 0 1 0 0.1 0 z"/>
      <circle class="w" cx="368" cy="162" r="26"/>
      <rect class="i1" x="318" y="132" width="34" height="18" rx="9" transform="rotate(24 335 141)"/>
      <path class="i1" d="M300 216 l52 84 h-104 z"/>
      <rect class="w" x="294" y="248" width="12" height="28"/>
      <circle class="w" cx="300" cy="288" r="7"/>
    """,
    "hokejs": """
      <g transform="rotate(-18 150 150)">
        <rect class="i1" x="140" y="20" width="22" height="200" rx="8"/>
        <path class="i1" d="M140 220 q0 26 -28 26 h-54 v-24 h50 q10 0 10 -12 z"/>
      </g>
      <g transform="rotate(16 260 150)">
        <rect class="i2" x="250" y="20" width="22" height="200" rx="8"/>
        <path class="i2" d="M272 220 q0 26 28 26 h54 v-24 h-50 q-10 0 -10 -12 z"/>
      </g>
      <ellipse class="i2" cx="110" cy="280" rx="58" ry="20"/>
      <rect class="i1" x="290" y="200" width="104" height="78" rx="10"/>
      <path class="w" d="M316 200 v78 M342 200 v78 M368 200 v78 M290 226 h104 M290 252 h104"
            fill="none" style="stroke-width:5"/>
    """,
    "basketbols": """
      <circle class="i1" cx="140" cy="170" r="92"/>
      <path class="wl" d="M48 170 h184 M140 78 v184"/>
      <path class="wl" d="M75 108 a130 130 0 0 1 0 124 M205 108 a130 130 0 0 0 0 124"/>
      <rect class="i2" x="280" y="60" width="112" height="78" rx="8"/>
      <rect class="w" x="306" y="82" width="60" height="38"/>
      <rect class="i1" x="296" y="138" width="80" height="14" rx="7"/>
      <path class="i2" d="M304 152 l8 66 M368 152 l-8 66 M320 152 l4 66 M352 152 l-4 66 M336 152 v66 M306 178 h60 M310 200 h52"
            fill="none" style="stroke-width:6"/>
    """,
    "futbols": """
      <circle class="i1" cx="135" cy="175" r="90"/>
      <path class="w2" d="M135 123 l42 30 -16 50 h-52 l-16 -50 z"/>
      <path class="wl" d="M135 85 v38 M93 153 l-44 -16 M177 153 l44 -16 M110 202 l-27 38 M160 202 l27 38"/>
      <rect class="i2" x="272" y="84" width="120" height="90" rx="8"/>
      <path class="w" d="M302 84 v90 M332 84 v90 M362 84 v90 M272 114 h120 M272 144 h120"
            fill="none" style="stroke-width:5"/>
      <path class="i2" d="M290 226 a30 30 0 1 1 24 52 l-52 8 -12 -32 z"/>
      <circle class="w" cx="312" cy="252" r="9"/>
    """,
    "slavenibas": f"""
      {_star(120, 120, 74, 'i1')}
      {_star(216, 210, 40, 'i2')}
      <rect class="i2" x="252" y="96" width="132" height="94" rx="14"/>
      <rect class="i2" x="272" y="80" width="44" height="22" rx="6"/>
      <circle class="w" cx="318" cy="143" r="32"/>
      <circle class="i1" cx="318" cy="143" r="18"/>
      <circle class="i1" cx="366" cy="112" r="7" style="fill:#fff"/>
      <path class="i1" d="M60 250 a36 32 0 0 1 72 0 z M158 250 a36 32 0 0 1 72 0 z"/>
      <rect class="i1" x="126" y="234" width="38" height="10" rx="5"/>
    """,
    "muzika": """
      <path class="i1" d="M160 46 l128 -26 v138 a36 28 0 1 1 -14 -24 V 66 l-100 20 v124 a36 28 0 1 1 -14 -24 z"/>
      <circle class="i2" cx="96" cy="244" r="52"/>
      <circle class="w" cx="96" cy="244" r="20"/>
      <circle class="i2" cx="96" cy="244" r="8"/>
      <path class="i2" d="M290 150 a72 72 0 0 1 104 64 v44 h-30 v-42 h30 M290 150 v108 h-30 v-44 a72 72 0 0 1 30 -64"
            fill="none" style="stroke-width:16"/>
      <rect class="i2" x="256" y="208" width="34" height="66" rx="12"/>
      <rect class="i2" x="360" y="208" width="34" height="66" rx="12"/>
    """,
    "kino": """
      <g transform="rotate(-6 150 170)">
        <rect class="i1" x="55" y="140" width="190" height="120" rx="10"/>
        <path class="i2" d="M55 140 l190 -44 16 44 z"/>
        <path class="w" d="M92 116 l34 -8 14 24 -34 8 z M162 100 l34 -8 14 24 -34 8 z"/>
      </g>
      <rect class="i2" x="292" y="60" width="92" height="200" rx="10"/>
      <rect class="w" x="308" y="80" width="60" height="44"/>
      <rect class="w" x="308" y="138" width="60" height="44"/>
      <rect class="w" x="308" y="196" width="60" height="44"/>
    """,
}

# --- section definitions ---------------------------------------------------
# Colors chosen to stay in the muted Dzīvesstils palette family while not
# clashing with the existing nine subsection colors.

SECTIONS = [
    # (section label, hashtag display, slug, base color, icon set)
    ("ZIŅAS", "#LATVIJĀ", "zinas_latvija", "#a5495a", "latvija"),
    ("ZIŅAS", "#ĀRZEMĒS", "zinas_arzemes", "#3d6e94", "arzemes"),
    ("ZIŅAS", "#EKONOMIKA", "zinas_ekonomika", "#3f7d5a", "ekonomika"),
    ("ZIŅAS", "#KRIMINĀLZIŅAS", "zinas_kriminalzinas", "#4d5560", "kriminalzinas"),
    ("SPORTS", "#HOKEJS", "sports_hokejs", "#71a9cd", "hokejs"),
    ("SPORTS", "#BASKETBOLS", "sports_basketbols", "#d98936", "basketbols"),
    ("SPORTS", "#FUTBOLS", "sports_futbols", "#5c9e4a", "futbols"),
    ("IZKLAIDE", "#SLAVENĪBAS", "izklaide_slavenibas", "#8e4f8e", "slavenibas"),
    ("IZKLAIDE", "#MŪZIKA", "izklaide_muzika", "#b83d6e", "muzika"),
    ("IZKLAIDE", "#KINO", "izklaide_kino", "#6f4fa8", "kino"),
]

SIZES = {  # name -> (width, height, layout)
    "1000x400": (1000, 400, "hero"),
    "1000x125": (1000, 125, "strip"),
    "800x500": (800, 500, "hero"),
    "800x250": (800, 250, "hero"),
}


def banner_html(section: str, tag: str, color: str, icon: str,
                width: int, height: int, layout: str) -> str:
    dark = shade(color, -0.16)
    light = shade(color, +0.14)
    if layout == "hero":
        bar_h = round(height * (0.25 if height >= 400 else 0.27))
        art_h = height - bar_h
        icon_scale = art_h / 300 * 0.92
        w, a = width, art_h
        swoosh = (f'<path d="M {w*0.68},0 '
                  f'C {w*0.585},{a*0.01} {w*0.552},{a*0.055} {w*0.565},{a*0.13} '
                  f'C {w*0.60},{a*0.30} {w*0.76},{a*0.48} {w},{a*0.55} '
                  f'L {w},0 Z" fill="#fff"/>')
        tag_size = round(bar_h * 0.42)
        sec_size = round(bar_h * 0.24)
        logo_size = round(bar_h * 0.46)
        body = f"""
  <div class="art" style="height:{art_h}px">
    <svg class="icons" viewBox="0 0 400 300"
         style="width:{round(400*icon_scale)}px;height:{round(300*icon_scale)}px"
         >{ICONS[icon]}</svg>
    <svg class="sw" width="{width}" height="{art_h}">{swoosh}</svg>
    <div class="sadarbiba" style="font-size:{max(12, round(height*0.045))}px">SADARBĪBĀ AR</div>
  </div>
  <div class="bar" style="height:{bar_h}px">
    <div class="logo" style="font-size:{logo_size}px">tv<span>3</span>.lv</div>
    <div class="label">
      <div class="sec" style="font-size:{sec_size}px"><span>/</span>{section}</div>
      <div class="tag" style="font-size:{tag_size}px">{tag}</div>
    </div>
  </div>"""
    else:  # strip 1000x125: white label block left, colored strip right
        block_w = round(width * 0.295)
        aw, a = width - block_w, height
        icon_scale = height / 300 * 1.35
        swoosh = (f'<path d="M {aw*0.70},0 '
                  f'C {aw*0.615},{a*0.02} {aw*0.585},{a*0.10} {aw*0.598},{a*0.24} '
                  f'C {aw*0.63},{a*0.52} {aw*0.78},{a*0.80} {aw},{a*0.92} '
                  f'L {aw},0 Z" fill="#fff"/>')
        body = f"""
  <div class="strip">
    <div class="block" style="width:{block_w}px">
      <div class="sec" style="font-size:{round(height*0.19)}px">tv3.lv<span>/</span>{section}</div>
      <div class="tag" style="font-size:{round(height*0.30)}px">{tag}</div>
    </div>
    <div class="art artstrip">
      <svg class="icons" viewBox="0 0 400 300"
           style="width:{round(400*icon_scale)}px;height:{round(300*icon_scale)}px"
           >{ICONS[icon]}</svg>
      <svg class="sw" width="{aw}" height="{height}">{swoosh}</svg>
      <div class="sadarbiba" style="font-size:{max(10, round(height*0.115))}px">SADARBĪBĀ AR</div>
    </div>
  </div>"""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
* {{ margin:0; box-sizing:border-box;
    font-family:"DejaVu Sans","Arial Narrow",sans-serif; }}
.banner {{ width:{width}px; height:{height}px; overflow:hidden; position:relative;
    display:flex; flex-direction:column; background:#fff; }}
.art {{ position:relative; background:{color}; overflow:hidden; flex:none; }}
.icons {{ position:absolute; right:-1%; bottom:-6%; }}
.i1 {{ fill:{dark}; }}
.i2 {{ fill:{light}; }}
.w  {{ fill:#fff; stroke:#fff; opacity:.9; }}
.w2 {{ fill:#fff; opacity:.55; }}
.wl {{ fill:none; stroke:#fff; stroke-width:8; opacity:.7; }}
.sw {{ position:absolute; top:0; right:0; }}
.sadarbiba {{ position:absolute; top:7%; right:2.4%; color:#111; font-weight:500;
    letter-spacing:.06em; }}
.bar {{ background:#fff; display:flex; align-items:center;
    justify-content:space-between; padding:0 2.6%; flex:none; }}
.logo {{ font-weight:800; color:#111; letter-spacing:-.02em; }}
.logo span {{ color:#e3000f; font-style:italic; }}
.label {{ text-align:right; line-height:1.12; }}
.sec {{ color:#111; font-weight:600; letter-spacing:.02em; }}
.sec span {{ color:#e3000f; font-weight:800; }}
.tag {{ color:{color}; font-weight:800; letter-spacing:.01em; }}
.strip {{ display:flex; height:100%; }}
.block {{ background:#fff; height:100%; display:flex; flex-direction:column;
    justify-content:center; padding:0 2.2%; flex:none; }}
.artstrip {{ flex:1; height:100%; }}
.artstrip .icons {{ right:16%; bottom:-46%; }}
.artstrip .sadarbiba {{ top:9%; right:1.6%; }}
</style></head><body><div class="banner">{body}</div></body></html>"""


PREVIEW_CSS = """
* { margin:0; box-sizing:border-box; font-family:"DejaVu Sans",sans-serif; }
body { background:#8a8a8a; width:1920px; height:1080px; position:relative;
       padding:60px 40px; }
h1 { position:absolute; top:24px; width:100%; left:0; text-align:center;
     color:#c9c9c9; font-size:26px; letter-spacing:.3em; }
.col { position:absolute; top:90px; }
.cap { color:#b5b5b5; font-size:22px; font-weight:bold; text-align:right;
       margin-top:10px; }
img { display:block; box-shadow:0 4px 24px rgba(0,0,0,.25); }
"""


def render_all(out_dir: Path) -> list[Path]:
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    sheets = []
    chromium = os.environ.get("PLAYWRIGHT_CHROMIUM", "")
    with sync_playwright() as p:
        browser = (p.chromium.launch(executable_path=chromium) if chromium
                   else p.chromium.launch())
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        for section, tag, slug, color, icon in SECTIONS:
            files = {}
            for size_name, (w, h, layout) in SIZES.items():
                html = banner_html(section, tag, color, icon, w, h, layout)
                f = out_dir / f"{slug}_{size_name}.html"
                f.write_text(html, encoding="utf-8")
                page.set_viewport_size({"width": w, "height": h})
                page.goto(f.as_uri())
                png = out_dir / f"{slug}_{size_name}.png"
                page.locator(".banner").screenshot(path=str(png))
                files[size_name] = png
            # designer-style preview sheet
            sheet_html = f"""<!doctype html><html><head><meta charset="utf-8">
<style>{PREVIEW_CSS}</style></head><body>
<h1>DESKTOP&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;
&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;MOBILE</h1>
<div class="col" style="left:45px">
  <img src="{files['1000x400'].name}" width="880">
  <div class="cap">1000x400 px</div>
  <img src="{files['1000x125'].name}" width="880" style="margin-top:120px">
  <div class="cap">1000x125 px</div>
</div>
<div class="col" style="left:1030px">
  <img src="{files['800x500'].name}" width="700">
  <div class="cap">800x500 px</div>
  <img src="{files['800x250'].name}" width="700" style="margin-top:60px">
  <div class="cap">800x250 px</div>
</div>
</body></html>"""
            sf = out_dir / f"_sheet_{slug}.html"
            sf.write_text(sheet_html, encoding="utf-8")
            page.set_viewport_size({"width": 1920, "height": 1080})
            page.goto(sf.as_uri())
            sheet_png = out_dir / f"sheet_{slug}.png"
            page.screenshot(path=str(sheet_png))
            sheets.append(sheet_png)
            print(f"rendered {slug}")
        browser.close()
    return sheets


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "banners_out")
    render_all(out)
    print(f"done -> {out}")
