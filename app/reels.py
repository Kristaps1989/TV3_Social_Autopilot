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
MAX_VIDEO_SECONDS = 45      # reels teaser: pietiek āķim, pārējais rakstā
STORY_MAX_SECONDS = 30      # video stories: API limits 60 s, labā prakse īsāk
MAX_VIDEO_BYTES = 300 * 1024 * 1024

# Feed lauki, kuros meklēt raksta videoklipu (tv3.lv/video 9:16 klipi)
VIDEO_KEYS = ("video", "video_url", "videoUrl", "video_src", "video_file",
              "videoFile", "mp4")


def article_video(article) -> str:
    """Raksta 9:16 videoklipa URL no feed datiem ('' ja nav)."""
    raw = article.raw_json or {}
    for key in ("_video_url", *VIDEO_KEYS):
        v = raw.get(key)
        if isinstance(v, dict):
            v = v.get("url") or v.get("src") or v.get("mp4") or ""
        if isinstance(v, str) and v.startswith("http"):
            return v
    return ""


def ffmpeg_bin() -> str:
    return os.environ.get("FFMPEG_BIN", "") or shutil.which("ffmpeg") or ""


def available() -> bool:
    return bool(ffmpeg_bin()) and cards.renderer_available()


def _point_frame_html(section: str, number: int, point: str,
                      bg_image: str = "") -> str:
    import html as _html

    style = cards.SECTION_STYLE.get(section) or cards.SECTION_STYLE["news"]
    color = style["color"]
    # raksta foto aptumšotā fonā tur uzmanību labāk nekā tukšs gradients
    bg = (f"background:url({_html.escape(bg_image, quote=True)}) "
          f"center/cover, {color};" if bg_image
          else f"background:linear-gradient(160deg, {color} 0%, #1c0d12 85%);")
    shade = ('<div style="position:absolute;inset:0;background:'
             'linear-gradient(160deg, rgba(12,6,16,.85), rgba(12,6,16,.62))">'
             '</div>' if bg_image else "")
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
* {{ margin:0; box-sizing:border-box; font-family:"DejaVu Sans",sans-serif; }}
.story {{ width:1080px; height:1920px; position:relative; overflow:hidden;
  {bg} }}
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
  {shade}
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


def _assemble(frames: list[Path], workdir: Path, out: Path,
              frame_seconds: float = FRAME_SECONDS,
              durations: list[float] | None = None) -> None:
    """durations: sekundes katram kadram atsevišķi (piem., īss intro/outro ap
    garākiem satura kadriem); bez tā visi kadri ir frame_seconds gari."""
    segments = []
    for i, png in enumerate(frames):
        seconds = (durations[i] if durations and i < len(durations)
                   else frame_seconds)
        per_frame = int(seconds * FPS)
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


def _fetch_video(url: str, dest: Path) -> str:
    """Returns the ffmpeg input: a downloaded local file, the URL itself for
    HLS streams (ffmpeg reads m3u8 directly), or a local path passed as-is."""
    if not url.startswith("http"):
        return url
    if ".m3u8" in url:
        return url
    import httpx

    with httpx.stream("GET", url, timeout=60, follow_redirects=True,
                      headers={"User-Agent": "TV3-Social-Autopilot/1.0"}) as resp:
        if resp.status_code != 200:
            raise RuntimeError(f"video lejupielāde neizdevās ({resp.status_code})")
        size = 0
        with dest.open("wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=1 << 20):
                size += len(chunk)
                if size > MAX_VIDEO_BYTES:
                    raise RuntimeError("video pārsniedz izmēra limitu")
                fh.write(chunk)
    return str(dest)


def _has_audio(path: Path) -> bool:
    proc = subprocess.run([ffmpeg_bin(), "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True, timeout=60)
    return "Audio:" in proc.stderr


def build_video_reel(video_url: str, out_dir: Path | None = None,
                     max_seconds: int = MAX_VIDEO_SECONDS) -> str:
    """Real video reel: the article's 9:16 clip, capped at max_seconds,
    normalised to 1080x1920 H.264/AAC, with the branded CTA end card appended
    so every reel closes on 'lasi tv3.lv'. Returns the local file path.
    Video stories reuse this with the shorter STORY_MAX_SECONDS cap."""
    out_dir = Path(out_dir or cards.CARDS_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"reel_{secrets.token_hex(6)}.mp4"
    common_v = ["-c:v", "libx264", "-preset", "veryfast"]
    common_a = ["-ar", "44100", "-ac", "2", "-c:a", "aac", "-b:a", "128k"]
    with tempfile.TemporaryDirectory(dir=out_dir) as tmp:
        workdir = Path(tmp)
        src = _fetch_video(video_url, workdir / "src.mp4")
        seg0 = workdir / "seg0.mp4"
        _run_ffmpeg([
            "-i", src, "-t", str(max_seconds),
            "-vf", ("scale=1080:1920:force_original_aspect_ratio=decrease,"
                    "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,"
                    f"fps={FPS},format=yuv420p"),
            "-map", "0:v:0", "-map", "0:a:0?",
            *common_v, *common_a, str(seg0)])
        if not _has_audio(seg0):
            fixed = workdir / "seg0a.mp4"
            _run_ffmpeg([
                "-i", str(seg0), "-f", "lavfi",
                "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-shortest", "-c:v", "copy",
                "-ar", "44100", "-ac", "2", "-c:a", "aac", "-b:a", "128k",
                str(fixed)])
            seg0 = fixed
        frame = _render_frames([_end_frame_html()], workdir)[0]
        seg1 = workdir / "seg1.mp4"
        _run_ffmpeg([
            "-loop", "1", "-i", str(frame), "-f", "lavfi",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-frames:v", str(int(FRAME_SECONDS * FPS)),
            "-vf", f"scale=1080:1920,fps={FPS},format=yuv420p",
            *common_v, *common_a, "-shortest", str(seg1)])
        lst = workdir / "list.txt"
        lst.write_text(f"file '{seg0}'\nfile '{seg1}'\n", encoding="utf-8")
        _run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(lst),
                     "-c", "copy", str(out)])
    log.info("video reel built: %s (source %s)", out.name, video_url[:80])
    return str(out)


def build_reel(title: str, section: str, image_url: str, points: list[str],
               out_dir: Path | None = None,
               max_points: int = MAX_POINTS,
               frame_seconds: float = FRAME_SECONDS,
               edge_seconds: float | None = None,
               include_cover: bool = True,
               include_end: bool = True,
               cover_images: list[str] | None = None,
               point_images: list[str] | None = None) -> str:
    """Render frames and assemble the MP4; returns the local file path.

    Teaseri: vāks + īsi punkti + CTA kadrs, 2.8 s kadrā. Digest (nedēļas
    TOP 5): punkti pa 6 s, lai garos virsrakstus ar datumu var izlasīt, un
    īss intro/outro (edge_seconds) — vāks dod kontekstu, beigu kadrs CTA,
    bet saturs paliek video centrā."""
    out_dir = Path(out_dir or cards.CARDS_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    edge = frame_seconds if edge_seconds is None else edge_seconds
    docs, durations = [], []
    if include_cover:
        if cover_images:
            docs.append(cards.build_mosaic_story_html(title, section,
                                                      cover_images))
        else:
            docs.append(cards.build_story_html(title, section, image_url))
        durations.append(edge)
    point_images = point_images or []
    for i, p in enumerate(points[:max_points], start=1):
        bg = point_images[i - 1] if i - 1 < len(point_images) else ""
        docs.append(_point_frame_html(section, i, p, bg_image=bg))
        durations.append(frame_seconds)
    if include_end:
        docs.append(_end_frame_html())
        durations.append(edge)
    out = out_dir / f"reel_{secrets.token_hex(6)}.mp4"
    with tempfile.TemporaryDirectory(dir=out_dir) as tmp:
        workdir = Path(tmp)
        frames = _render_frames(docs, workdir)
        _assemble(frames, workdir, out, durations=durations)
    log.info("reel built: %s (%d frames, %.0fs total)",
             out.name, len(docs), sum(durations))
    return str(out)
