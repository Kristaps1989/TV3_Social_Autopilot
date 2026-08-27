"""Slideshow Reel builder: branded 9:16 frames -> short MP4 via ffmpeg.

A reel is an explainer teaser built from content we already have: a cover
frame (the story layout with the title plate), 2-3 point frames from the
AI's card_points, and a closing frame that is pure CTA — read the full
story on tv3.lv. A slow Ken Burns zoom keeps the stills alive. Audio is a
silent track: Meta's music library is not licensable through the API.
"""
from __future__ import annotations

import logging
import os
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

from app import cards

log = logging.getLogger(__name__)

FPS = 25
FRAME_SECONDS = 2.8
MAX_POINTS = 3


def ffmpeg_bin() -> str:
    return os.environ.get("FFMPEG_BIN", "") or shutil.which("ffmpeg") or ""


def available() -> bool:
    return bool(ffmpeg_bin()) and cards.renderer_available()


def _point_frame_html(section: str, number: int, point: str) -> str:
    import html as _html

    style = cards.SECTION_STYLE.get(section) or cards.SECTION_STYLE["news"]
    color = style["color"]
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
* {{ margin:0; box-sizing:border-box; font-family:"DejaVu Sans",sans-serif; }}
.story {{ width:1080px; height:1920px; position:relative; overflow:hidden;
  background:linear-gradient(160deg, {color} 0%, #1c0d12 85%); }}
.brand {{ position:absolute; top:200px; right:48px; background:#fff;
          border-radius:14px; padding:14px 22px; }}
.num {{ position:absolute; top:480px; left:72px; font-size:260px; font-weight:bold;
        color:rgba(255,255,255,.22); line-height:1; }}
.point {{ position:absolute; top:760px; left:72px; max-width:920px;
          font-size:76px; line-height:1.22; font-weight:bold; color:#fff; }}
.linkpill {{ position:absolute; bottom:252px; left:56px; background:#fff;
             color:#e3000f; font-size:48px; font-weight:bold;
             padding:22px 52px; border-radius:99px;
             box-shadow:0 8px 30px rgba(0,0,0,.35); }}
.linkpill svg {{ vertical-align:-7px; margin-right:16px; }}
</style></head><body>
<div class="story">
  <div class="brand">{cards._logo(52)}</div>
  <div class="num">{number}</div>
  <div class="point">{_html.escape(point)}</div>
  <div class="linkpill">{cards._LINK_ICON}tv3.lv</div>
</div>
</body></html>"""


def _end_frame_html() -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
* {{ margin:0; box-sizing:border-box; font-family:"DejaVu Sans",sans-serif; }}
.story {{ width:1080px; height:1920px; position:relative; overflow:hidden;
  background:#e3000f; display:flex; flex-direction:column; align-items:center;
  justify-content:center; gap:56px; }}
.chip {{ background:#fff; border-radius:20px; padding:26px 40px; }}
h1 {{ font-size:88px; font-weight:bold; color:#fff; text-align:center;
      max-width:900px; line-height:1.15; }}
.linkpill {{ background:#fff; color:#e3000f; font-size:70px; font-weight:bold;
             padding:30px 70px; border-radius:99px;
             box-shadow:0 10px 40px rgba(0,0,0,.3); }}
.linkpill svg {{ vertical-align:-8px; margin-right:20px; width:62px; height:62px; }}
.sub {{ font-size:44px; color:#fff; opacity:.92; font-weight:bold; }}
</style></head><body>
<div class="story">
  <div class="chip">{cards._logo(72)}</div>
  <h1>Pilns stāsts portālā</h1>
  <div class="linkpill">{cards._LINK_ICON}tv3.lv</div>
  <div class="sub">Lasi visu rakstā →</div>
</div>
</body></html>"""


def _render_frames(docs: list[str], out_dir: Path) -> list[Path]:
    from playwright.sync_api import sync_playwright

    chromium = os.environ.get("PLAYWRIGHT_CHROMIUM", "")
    paths: list[Path] = []
    with sync_playwright() as p:
        browser = (p.chromium.launch(executable_path=chromium) if chromium
                   else p.chromium.launch())
        page = browser.new_page(viewport={"width": 1080, "height": 1920})
        for i, doc in enumerate(docs):
            tmp = out_dir / f"frame{i}.html"
            tmp.write_text(doc, encoding="utf-8")
            page.goto(tmp.as_uri(), timeout=30000)
            page.wait_for_timeout(600)
            out = out_dir / f"frame{i}.png"
            page.locator(".story").screenshot(path=str(out), timeout=15000)
            paths.append(out)
            tmp.unlink(missing_ok=True)
        browser.close()
    return paths


def _run_ffmpeg(args: list[str]) -> None:
    proc = subprocess.run([ffmpeg_bin(), "-y", "-loglevel", "error", *args],
                          capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[-400:]}")


def _assemble(frames: list[Path], workdir: Path, out: Path) -> None:
    per_frame = int(FRAME_SECONDS * FPS)
    segments = []
    for i, png in enumerate(frames):
        seg = workdir / f"seg{i}.mp4"
        # upscale first so the zoom pans over subpixels instead of jittering
        _run_ffmpeg([
            "-loop", "1", "-i", str(png),
            "-vf", ("scale=1296:2304,"
                    f"zoompan=z='min(1+0.0016*on,1.12)':x='(iw-iw/zoom)/2':"
                    f"y='(ih-ih/zoom)/2':d={per_frame}:s=1080x1920:fps={FPS},"
                    "format=yuv420p"),
            "-frames:v", str(per_frame),
            "-c:v", "libx264", "-preset", "veryfast", str(seg)])
        segments.append(seg)
    lst = workdir / "list.txt"
    lst.write_text("".join(f"file '{s}'\n" for s in segments), encoding="utf-8")
    _run_ffmpeg([
        "-f", "concat", "-safe", "0", "-i", str(lst),
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-shortest", "-c:v", "copy", "-c:a", "aac", "-b:a", "64k", str(out)])


def build_reel(title: str, section: str, image_url: str, points: list[str],
               out_dir: Path | None = None) -> str:
    """Render frames and assemble the MP4; returns the local file path."""
    out_dir = Path(out_dir or cards.CARDS_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    docs = [cards.build_story_html(title, section, image_url)]
    docs += [_point_frame_html(section, i, p)
             for i, p in enumerate(points[:MAX_POINTS], start=1)]
    docs.append(_end_frame_html())
    out = out_dir / f"reel_{secrets.token_hex(6)}.mp4"
    with tempfile.TemporaryDirectory(dir=out_dir) as tmp:
        workdir = Path(tmp)
        frames = _render_frames(docs, workdir)
        _assemble(frames, workdir, out)
    log.info("reel built: %s (%d frames)", out.name, len(docs))
    return str(out)
