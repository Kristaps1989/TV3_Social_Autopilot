"""Slideshow Reel builder: branded 9:16 frames -> short MP4 via ffmpeg.

A reel is an explainer teaser built from content we already have: a cover
frame (the story layout with the title plate), 2-3 point frames from the
AI's card_points, and a closing frame that is pure CTA — read the full
story on tv3.lv. A slow Ken Burns zoom keeps the stills alive.

Audio is a silent track by default — Meta's music library is not licensable
through the API — but a reel can carry a voice-over instead.

Ierunu sinhronizējam PA KADRIEM, ne pa visu lenti: katram kadram tiek
sintezēts savs teikums, un kadrs ir tieši tik garš, cik tā ieruna. Viens
gabals runas pār visu lenti izklausījās salauzts — attēls jau bija pie CTA,
kamēr balss vēl stāstīja iepriekšējo domu.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

from app import cards

log = logging.getLogger(__name__)

FPS = 25
FRAME_SECONDS = 2.8
SECTION_FRAME_SECONDS = 5.5  # sadaļas kadrā ir teikumi, ne viens punkts
# Ken Burns tuvinājums apgriež kadru no malām: pie z redzama ir 1/z daļa,
# tāpēc mala pazūd. 1.08 nozīmē ~40 px no 1080 katrā pusē — un kadru
# izkārtojums tur malās SAFE_INSET brīvu vietu, lai tur nekas nav.
MAX_ZOOM = 1.08
SAFE_INSET = 64             # cik iekšā no malas liekam tekstu lentes kadros
MAX_POINTS = 3
MAX_VIDEO_SECONDS = 45      # reels teaser: pietiek āķim, pārējais rakstā
STORY_MAX_SECONDS = 30      # video stories: API limits 60 s, labā prakse īsāk
STORY_API_MAX_SECONDS = 60  # Facebook video stāsta griesti
MAX_VIDEO_BYTES = 300 * 1024 * 1024
# ~40 s runas ziņu tempā (ap 135 vārdiem minūtē); garāka ieruna vairs nav
# teaseris. Īsto tempu konkrētai lentei rāda priekšskatījums — tas rēķina to
# no izmērītā runas garuma, nevis no šī pieņēmuma.
VOICE_MAX_WORDS = 90
VOICE_TAIL_SECONDS = 0.6    # CTA kadrs paliek redzams pēc pēdējā vārda
VOICE_MAX_SECONDS = 60

# --- ierunas un kadra saskaņošana ------------------------------------------
# Skatītājam vajag mirkli, lai pamanītu jauno kadru, pirms tajā sāk runāt;
# un elpu pēc pēdējā vārda, pirms kadrs nomainās. Bez tām abām nodaļas
# saplūst vienā gabalā, un tieši tas skanēja kā steiga.
VOICE_LEAD_SECONDS = 0.35   # klusums kadra sākumā
VOICE_GAP_SECONDS = 0.5     # elpa aiz pēdējā vārda
MIN_FRAME_SECONDS = 2.2     # arī viena teikuma nodaļa nedrīkst pazibēt
END_VOICE_TAIL = 1.0        # CTA kadrs paliek stāvam pēc noslēguma teikuma

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


_URL_RE = re.compile(r"https?://\S+|\b\w+\.(?:lv|com|eu)/\S*")
_SPOKEN_JUNK_RE = re.compile(r"[()\[\]{}<>*_#|]+")


def voice_script(text: str, max_words: int = VOICE_MAX_WORDS) -> str:
    """Ierunas teksts, sagatavots runāšanai ('' ja nav ko runāt).

    Balss nolasa visu, kas tekstā ir: saiti tā izrunā pa burtiem, iekavas
    pārtrauc plūdumu. Tāpēc tie te tiek izņemti, nevis atstāti runātājam.
    Garumu griežam pie teikuma robežas — pusteikums balsī skan kā kļūda.
    """
    text = _URL_RE.sub("", text or "")
    text = _SPOKEN_JUNK_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    words = text.split()
    if len(words) < 12:
        return ""   # par īsu, lai būtu ieruna; labāk klusa lente
    if len(words) > max_words:
        text = " ".join(words[:max_words])
        cut = max(text.rfind(". "), text.rfind("! "), text.rfind("? "))
        text = text[:cut + 1] if cut > 40 else text.rstrip(" ,;:") + "."
    return text.strip()


def spoken_line(text: str, max_words: int = 34) -> str:
    """Viena runājama rinda (virsraksts, āķis) — bez saitēm un pieturzīmēm,
    ko balss mēģinātu izrunāt pa burtiem.

    Atšķirībā no `voice_script` te nav minimālā garuma: virsraksts ir īss
    pēc būtības, un tieši tas ir lentes atklāšanas teikums.
    """
    text = _URL_RE.sub("", text or "")
    text = _SPOKEN_JUNK_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    words = text.split()
    if len(words) > max_words:
        text = " ".join(words[:max_words]).rstrip(" ,;:") + "."
    if text and text[-1] not in ".!?":
        text += "."
    return text


def end_voice_text(rules: dict | None = None) -> str:
    """Noslēguma teikums — aicinājums uz portālu.

    MI atruna te pēc noklusējuma NEskan: marķējums ir redzams uz kadra,
    pilnā tekstā noslēguma kadrā un parakstā, un izrunāts tas nāca kā
    liekais teikums aiz aicinājuma. Kam vajag arī skaļi — `ai_disclosure_spoken`
    Noteikumos (sk. app/disclosure.py).
    """
    from app import disclosure

    parts = ["Pilnu stāstu lasi tv3.lv."]
    note = disclosure.spoken(rules)
    if note:
        parts.append(note)
    return " ".join(parts)


def media_duration(path: str | Path) -> float:
    """Ieraksta garums sekundēs (0.0, ja neizdodas nolasīt)."""
    proc = subprocess.run([ffmpeg_bin(), "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True, timeout=60)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", proc.stderr)
    if not m:
        return 0.0
    h, mnt, sec = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(sec)


def _stretch_to_voice(durations: list[float], voice_seconds: float) -> list[float]:
    """Kadru garumi, izstiepti tā, lai ieruna paspētu izskanēt.

    Proporcionāli, nevis pieliekot visu pēdējam kadram: citādi CTA kadrs
    stāv desmit sekundes, kamēr saturs pazib garām.
    """
    total = sum(durations)
    target = min(voice_seconds + VOICE_TAIL_SECONDS, VOICE_MAX_SECONDS)
    if not durations or total <= 0 or target <= total:
        return durations
    factor = target / total
    return [d * factor for d in durations]


def has_voice(post) -> bool:
    """Vai šajā lentē tiešām skan balss.

    Receptes `voice_script` vien to nepasaka: scenārijs tur ir arī tad, kad
    sintēze neizdevās vai atslēgas nebija. Statistikā tie ir divi dažādi
    ieraksti, tāpēc skatāmies uz karogu, ko uzliek būvēšana.
    """
    return bool(((getattr(post, "extra", None) or {}).get("recipe")
                 or {}).get("voiced"))


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
.brand {{ position:absolute; top:200px; right:{SAFE_INSET + 48}px; background:#fff;
          border-radius:14px; padding:14px 22px; }}
.num {{ position:absolute; top:480px; left:{SAFE_INSET + 72}px; font-size:260px;
        font-weight:bold; color:rgba(255,255,255,.22); line-height:1; }}
.point {{ position:absolute; top:760px; left:{SAFE_INSET + 72}px; max-width:856px;
          font-size:76px; line-height:1.22; font-weight:bold; color:#fff; }}
.linkpill {{ position:absolute; bottom:252px; left:{SAFE_INSET + 56}px;
             background:#fff; color:#e3000f; font-size:48px; font-weight:bold;
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


def _bg_layers(color: str, bg_image: str, blur_image: str) -> tuple[str, str]:
    """(CSS fonam, papildu slāņu HTML) sadaļas/vāka kadram.

    Secība ir apzināta: īsts foto > izpludināta photopost grafika > plakans
    gradients. Iepriekš lente pie pirmā roba nokrita uzreiz līdz gradientam,
    un iznāca lente bez nevienas bildes, lai gan rakstā attēls BIJA — tikai
    ar iecepto virsrakstu, kas der vienīgi kā faktūra.
    """
    import html as _html

    if bg_image:
        return (f"background:url({_html.escape(bg_image, quote=True)}) "
                f"center/cover, {color};", "")
    if blur_image:
        return (f"background:linear-gradient(160deg, {color} 0%, #1c0d12 85%);",
                f'<div class="blurbg" style="background-image:'
                f'url({_html.escape(blur_image, quote=True)})"></div>')
    return (f"background:linear-gradient(160deg, {color} 0%, #1c0d12 85%);", "")


_BLUR_CSS = """.blurbg { position:absolute; inset:-80px; background-size:cover;
  background-position:center; filter:blur(34px) brightness(.72) saturate(1.15); }
"""


def _progress_html(step: int, total: int) -> str:
    """Nodaļu josla kadra augšā: cik tālu stāsts ir aizgājis.

    Lentē nav švīkošanas, tāpēc skatītājs nezina, vai priekšā vēl ir minūte
    vai divas sekundes — un nezinot mēdz aiziet. Josla to pasaka klusi.
    """
    if total < 2:
        return ""
    cells = "".join(
        f'<i class="{"on" if i <= step else ""}"></i>' for i in range(1, total + 1))
    return f'<div class="prog">{cells}</div>'


def _section_frame_html(section: str, number: int, title: str, body: str,
                        bg_image: str = "", blur_image: str = "",
                        total: int = 0, rules: dict | None = None,
                        mark_ai: bool = False) -> str:
    """Sadaļas kadrs lentei: foto fonā, balts panelis, sarkana nodaļas rinda
    un teikumi zem tās.

    Virsraksts te ir NODAĻAS marķieris, nevis otrs virsraksts: balss to vairs
    nelasa, tā runā tikai teikumus. Kad balss nolasīja abus, klausītājam vienu
    un to pašu domu pateica divreiz.

    Viss teksts SAFE_INSET drošajā zonā — Ken Burns tuvinājums malas apgriež.
    """
    import html as _html

    from app import disclosure

    style = cards.SECTION_STYLE.get(section) or cards.SECTION_STYLE["news"]
    color = style["color"]
    bg, blur_layer = _bg_layers(color, bg_image, blur_image)
    head = (title or "").strip()
    head_html = (f'<div class="chapter">{_html.escape(head)}</div>'
                 if head else "")
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
* {{ margin:0; box-sizing:border-box; font-family:"DejaVu Sans",sans-serif; }}
.story {{ width:1080px; height:1920px; position:relative; overflow:hidden;
  {bg} }}
{_BLUR_CSS}
.veil {{ position:absolute; inset:0; background:rgba(10,8,14,.32); }}
.brand {{ position:absolute; top:200px; right:{SAFE_INSET + 48}px;
          background:#fff; border-radius:14px; padding:14px 22px; }}
.prog {{ position:absolute; top:236px; left:{SAFE_INSET + 56}px;
         display:flex; gap:10px; width:420px;
         /* uz gaiša foto balta josla uz balta pazūd — ēna to notur
            salasāmu arī tad, kad kadrā ir debesis vai sniegs */
         filter:drop-shadow(0 2px 7px rgba(0,0,0,.65)); }}
.prog i {{ flex:1; height:9px; border-radius:99px;
           background:rgba(255,255,255,.48); }}
.prog i.on {{ background:#fff; }}
.panelwrap {{ position:absolute; top:420px; bottom:520px;
              left:{SAFE_INSET + 40}px; right:{SAFE_INSET + 40}px;
              display:flex; align-items:center; justify-content:center; }}
.panel {{ background:rgba(255,255,255,.96);
          backdrop-filter:blur(16px) saturate(.75);
          -webkit-backdrop-filter:blur(16px) saturate(.75);
          border-radius:22px; padding:60px 58px; text-align:center;
          box-shadow:0 16px 60px rgba(0,0,0,.32); }}
.chapter {{ color:#e3000f; font-size:{cards.fit_size(head, 40)}px;
            font-weight:bold; letter-spacing:.04em; line-height:1.2;
            padding-bottom:26px; margin-bottom:30px;
            border-bottom:5px solid rgba(227,0,15,.24); }}
.panel p {{ color:#20242c; font-size:{cards.body_fit(body, 50)}px;
            line-height:1.4; font-weight:600; }}
.linkpill {{ position:absolute; bottom:252px; left:{SAFE_INSET + 56}px;
             background:#fff; color:#e3000f; font-size:48px; font-weight:bold;
             padding:22px 52px; border-radius:99px;
             box-shadow:0 8px 30px rgba(0,0,0,.35); }}
.linkpill svg {{ vertical-align:-7px; margin-right:16px; }}
{disclosure.badge_css(left=SAFE_INSET + 56, bottom=170, size=28) if mark_ai else ""}
</style></head><body>
<div class="story">
  {blur_layer}
  <div class="veil"></div>
  <div class="brand">{cards._logo(52)}</div>
  {_progress_html(number, total)}
  <div class="panelwrap"><div class="panel">
    {head_html}
    <p>{_html.escape(body)}</p>
  </div></div>
  <div class="linkpill">{cards._LINK_ICON}tv3.lv</div>
  {disclosure.badge_html(rules) if mark_ai else ""}
</div>
</body></html>"""


def _end_frame_html(rules: dict | None = None,
                    mark_ai: bool = False) -> str:
    """Noslēguma kadrs: CTA uz portālu un ES MI akta atruna pilnā tekstā.

    Marķējumam jābūt skaidram un pamanāmam (Regula 2024/1689, 50. pants).
    Uz satura kadriem tas ir neliela zīmīte, lai neaizsedz stāstu; te, kur
    vietas ir, tas stāv pilnā teikumā.
    """
    from app import disclosure

    note = disclosure.text(rules) if mark_ai else ""
    note_html = (f'<div class="note"><b>MI</b><span>{note}</span></div>'
                 if note else "")
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
.note {{ position:absolute; left:{SAFE_INSET + 60}px; right:{SAFE_INSET + 60}px;
         bottom:250px; display:flex; align-items:center; gap:18px;
         justify-content:center; color:#fff; font-size:31px; font-weight:600;
         line-height:1.3; text-align:left;
         border-top:3px solid rgba(255,255,255,.4); padding-top:26px; }}
.note b {{ background:#fff; color:#e3000f; border-radius:9px;
           padding:4px 12px; font-size:30px; flex:none; }}
</style></head><body>
<div class="story">
  <div class="chip">{cards._logo(72)}</div>
  <h1>Pilns stāsts portālā</h1>
  <div class="linkpill">{cards._LINK_ICON}tv3.lv</div>
  <div class="sub">Lasi visu rakstā →</div>
  {note_html}
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
            # fiksēts miegs bija par īsu lēnam CDN — kadrs tad iznāca kā
            # tukšs krāsas laukums bez foto. cards._settle gaida, līdz tīkls
            # norimst; tā pati kļūda kartītēs jau bija salabota, lentēs ne.
            cards._settle(page)
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


_AAC = ["-ar", "44100", "-ac", "2", "-c:a", "aac", "-b:a", "128k"]


def frame_seconds_for(speech_seconds: float, last: bool = False) -> float:
    """Cik gara jābūt kadram, lai tā ieruna tajā ietilptu bez steigas."""
    if speech_seconds <= 0:
        return 0.0
    tail = END_VOICE_TAIL if last else VOICE_GAP_SECONDS
    return max(MIN_FRAME_SECONDS,
               VOICE_LEAD_SECONDS + speech_seconds + tail)


def _audio_segment(voice: str, seconds: float, dest: Path) -> None:
    """Viena kadra skaņas celiņš, tieši `seconds` garš.

    Klusums priekšā dod skatītājam mirkli ieraudzīt kadru; apad aizpilda
    atlikumu, lai celiņš beigtos tieši ar kadru — tieši šī precizitāte
    notur attēlu un balsi kopā visas lentes garumā.
    """
    if voice:
        _run_ffmpeg([
            "-i", str(voice),
            "-af", (f"adelay=delays={int(VOICE_LEAD_SECONDS * 1000)}:all=1,"
                    "apad"),
            "-t", f"{seconds:.3f}", *_AAC, str(dest)])
    else:
        _run_ffmpeg([
            "-f", "lavfi",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t", f"{seconds:.3f}", *_AAC, str(dest)])


def _concat(paths: list[Path], workdir: Path, out: Path, name: str,
            args: list[str]) -> None:
    lst = workdir / f"{name}.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in paths), encoding="utf-8")
    _run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(lst), *args, str(out)])


def plan_durations(durations: list[float], voices: list[str] | None,
                   speech: list[float] | None = None) -> list[float]:
    """Kadru garumi, kad katram kadram ir sava ieruna.

    Agrāk visu kadru garumus mēroja proporcionāli vienam runas gabalam. Tas
    ir pareizi tikai tad, ja katra nodaļa aizņem tieši tik lielu daļu runas,
    cik liela daļa kadru tai pieder — un praksē nekad neaizņēma. Tāpēc attēls
    aizskrēja priekšā, un pēdējais kadrs ar CTA stāvēja, kamēr balss vēl
    stāstīja iepriekšējo nodaļu. Tagad kadru nosaka tā paša kadra ieruna.
    """
    if not voices:
        return durations
    out: list[float] = []
    last_voiced = max((i for i, v in enumerate(voices) if v), default=-1)
    for i, base in enumerate(durations):
        v = voices[i] if i < len(voices) else ""
        secs = (speech[i] if speech and i < len(speech)
                else (media_duration(v) if v else 0.0))
        need = frame_seconds_for(secs, last=(i == last_voiced))
        out.append(need if need > 0 else base)
    return out


def _assemble(frames: list[Path], workdir: Path, out: Path,
              frame_seconds: float = FRAME_SECONDS,
              durations: list[float] | None = None,
              voice: str | Path | None = None,
              voices: list[str] | None = None) -> float:
    """durations: sekundes katram kadram atsevišķi (piem., īss intro/outro ap
    garākiem satura kadriem); bez tā visi kadri ir frame_seconds gari.

    voices: ieruna KATRAM kadram atsevišķi — kadrs tad ir tieši tik garš, cik
    tā paša kadra runa, un attēls ar balsi nekur neaizšķiras.

    voice: viens runas fails pār visu lenti (vecais ceļš — nedēļas digests un
    manuāli pieprasītās lentes, kur nodaļu dalījuma nav). Kadri tiek izstiepti
    proporcionāli, lai balss netiktu nogriezta pusvārdā.
    """
    voiced = [v for v in (voices or []) if v]
    if voiced:
        durations = plan_durations(durations or [frame_seconds] * len(frames),
                                   voices)
    elif voice:
        seconds = media_duration(voice)
        if seconds > 0:
            base = durations or [frame_seconds] * len(frames)
            durations = _stretch_to_voice(base, seconds)
        else:
            log.warning("voice track %s has no readable duration — silent reel",
                        voice)
            voice = None
    segments, asegments = [], []
    total_frames = 0
    for i, png in enumerate(frames):
        seconds = (durations[i] if durations and i < len(durations)
                   else frame_seconds)
        per_frame = max(1, int(round(seconds * FPS)))
        total_frames += per_frame
        seg = workdir / f"seg{i}.mp4"
        # upscale first so the zoom pans over subpixels instead of jittering
        _run_ffmpeg([
            "-loop", "1", "-i", str(png),
            "-vf", ("scale=1296:2304,"
                    f"zoompan=z='min(1+0.0016*on,{MAX_ZOOM})':x='(iw-iw/zoom)/2':"
                    f"y='(ih-ih/zoom)/2':d={per_frame}:s=1080x1920:fps={FPS},"
                    "format=yuv420p"),
            "-frames:v", str(per_frame),
            "-c:v", "libx264", "-preset", "veryfast", str(seg)])
        segments.append(seg)
        if voiced:
            aseg = workdir / f"aseg{i}.m4a"
            _audio_segment(voices[i] if i < len(voices) else "",
                           per_frame / FPS, aseg)
            asegments.append(aseg)
    video_seconds = total_frames / FPS
    if voiced:
        # skaņu liekam kopā no tiem pašiem gabaliem, kas kadrus: katrs celiņš
        # ir tieši sava kadra garumā, tāpēc kopsummas sakrīt pa kadru robežām
        # un noiet nav no kā rasties
        video = workdir / "video.mp4"
        audio = workdir / "audio.m4a"
        _concat(segments, workdir, video, "vlist", ["-c", "copy"])
        _concat(asegments, workdir, audio, "alist", _AAC)
        _run_ffmpeg(["-i", str(video), "-i", str(audio),
                     "-map", "0:v", "-map", "1:a", "-c", "copy",
                     "-t", f"{video_seconds:.3f}", str(out)])
    elif voice:
        # apad pieliek klusumu aiz pēdējā vārda, lai CTA kadrs nepaliek bez
        # skaņas celiņa. Beigas nosaka -t, nevis -shortest: apad ģenerē
        # bezgalīgu klusumu, un -shortest caur filtru neizplatās — ffmpeg
        # tad kodē mūžīgi.
        lst = workdir / "list.txt"
        lst.write_text("".join(f"file '{s}'\n" for s in segments),
                       encoding="utf-8")
        _run_ffmpeg([
            "-f", "concat", "-safe", "0", "-i", str(lst), "-i", str(voice),
            "-map", "0:v", "-map", "1:a", "-af", "apad",
            "-t", f"{video_seconds:.3f}",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", str(out)])
    else:
        lst = workdir / "list.txt"
        lst.write_text("".join(f"file '{s}'\n" for s in segments),
                       encoding="utf-8")
        _run_ffmpeg([
            "-f", "concat", "-safe", "0", "-i", str(lst),
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-shortest", "-c:v", "copy", "-c:a", "aac", "-b:a", "64k", str(out)])
    return video_seconds


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


def chapter_voice(sec: dict) -> str:
    """Ko balss saka pār vienu nodaļas kadru.

    Tikai tekstu, nevis virsrakstu+tekstu. Virsraksts jau stāv uz ekrāna
    sarkanā rindā, un, to vēl nolasot, klausītājs vienu domu dzird divreiz —
    tieši tā skanēja liekvārdība, ko pamanīja redakcija.
    """
    body = str(sec.get("voice") or sec.get("body") or "").strip()
    head = str(sec.get("title") or "").strip().rstrip(".!?")
    # ja AI teksts tomēr sākas ar pašu virsrakstu, atkārtojumu izņemam te
    if head and body.lower().startswith(head.lower()):
        rest = body[len(head):].lstrip(" .,:;—-")
        if len(rest) > 40:
            body = rest
    return body


def plan_beats(title: str, sections: list[dict], points: list[str],
               cover_voice: str = "", end_voice: str = "",
               include_cover: bool = True, include_end: bool = True,
               max_points: int = MAX_POINTS,
               frame_seconds: float = FRAME_SECONDS,
               edge_seconds: float | None = None,
               point_images: list[str] | None = None) -> list[dict]:
    """Lentes plāns: pa vienam ierakstam katram kadram, pareizā secībā.

    Katrs kadrs nes SAVU ierunu un savu ilgumu. Agrāk teksti un kadri bija
    divi paralēli saraksti, kurus vajadzēja turēt vienā garumā ar rokām —
    un tieši tur radās nesakritība: kadrus apgriezām īsākus, bet HTML jau
    bija uzzīmēts ar veco kopskaitu, tāpēc progresa josla rādīja «1 no 3»
    lentē, kurā nodaļu bija divas.
    """
    edge = frame_seconds if edge_seconds is None else edge_seconds
    imgs = point_images or []
    beats: list[dict] = []
    if include_cover:
        beats.append({"kind": "cover", "text": cover_voice, "duration": edge})
    used = list((sections or [])[:max_points])
    if sections:
        for i, sec in enumerate(used):
            beats.append({
                "kind": "section", "sec": sec,
                "bg": imgs[i % len(imgs)] if imgs else "",
                "text": chapter_voice(sec),
                "duration": max(frame_seconds, SECTION_FRAME_SECONDS)})
    else:
        for i, point in enumerate(points[:max_points]):
            beats.append({
                "kind": "point", "point": point,
                "bg": imgs[i] if i < len(imgs) else "",
                "text": "", "duration": frame_seconds})
    if include_end:
        beats.append({"kind": "end", "text": end_voice, "duration": edge})
    return beats


def _trim_beats(beats: list[dict], budget: float = VOICE_MAX_SECONDS) -> int:
    """Izmet PĒDĒJĀS nodaļas, kamēr lente ietilpst budžetā. Atgriež, cik izmests.

    Platformai ir griesti, un tos var sasniegt divējādi: nogriežot balsi
    pusvārdā vai atmetot pēdējo nodaļu veselu. Otrais ir godīgāks — stāsts
    beidzas pie nodaļas robežas, nevis pie pusteikuma.
    """
    dropped = 0
    while sum(b["duration"] for b in beats) > budget:
        content = [i for i, b in enumerate(beats)
                   if b["kind"] in ("section", "point")]
        if len(content) <= 1:
            break
        del beats[content[-1]]
        dropped += 1
    return dropped


def _beat_html(beat: dict, step: int, total: int, section: str,
               title: str, image_url: str, blur_image: str,
               cover_images: list[str] | None, rules: dict | None,
               mark_ai: bool = False) -> str:
    """Viena kadra HTML. step/total ir pozīcija VISĀ lentē, ne tikai starp
    nodaļām: skatītājs skaita kadrus, kurus redz, nevis tos, kurus mēs
    saucam par saturu — «1 no 3» otrajā kadrā no pieciem ir maldinoši."""
    kind = beat["kind"]
    if kind == "cover":
        if cover_images:
            return cards.build_mosaic_story_html(title, section, cover_images)
        return cards.build_story_html(title, section, image_url,
                                      inset=SAFE_INSET, blur_image=blur_image,
                                      ai_badge=mark_ai)
    if kind == "section":
        sec = beat["sec"]
        return _section_frame_html(section, step, sec.get("title", ""),
                                   sec.get("body", ""), bg_image=beat["bg"],
                                   blur_image=blur_image, total=total,
                                   rules=rules, mark_ai=mark_ai)
    if kind == "point":
        return _point_frame_html(section, step, beat["point"],
                                 bg_image=beat["bg"])
    return _end_frame_html(rules, mark_ai)


def build_reel(title: str, section: str, image_url: str, points: list[str],
               out_dir: Path | None = None,
               max_points: int = MAX_POINTS,
               frame_seconds: float = FRAME_SECONDS,
               edge_seconds: float | None = None,
               include_cover: bool = True,
               include_end: bool = True,
               cover_images: list[str] | None = None,
               point_images: list[str] | None = None,
               voice: str | Path | None = None,
               sections: list[dict] | None = None,
               blur_image: str = "",
               cover_voice: str = "",
               end_voice: str = "",
               synth=None,
               rules: dict | None = None,
               report: dict | None = None) -> str:
    """Render frames and assemble the MP4; returns the local file path.

    Teaseri: vāks + nodaļas + CTA kadrs. Digest (nedēļas TOP 5): punkti pa
    6 s, lai garos virsrakstus ar datumu var izlasīt, un īss intro/outro
    (edge_seconds) — vāks dod kontekstu, beigu kadrs CTA, bet saturs paliek
    video centrā.

    Kārtība ir svarīga: vispirms plāns (`plan_beats`), tad ieruna un
    apgriešana, un TIKAI TAD kadru HTML. Kad kadrus zīmēja pirms
    apgriešanas, izdzīvojušie kadri nesa veco kopskaitu, un progresa josla
    solīja nodaļas, kuru lentē vairs nebija.

    cover_voice / end_voice: ko balss saka pār vāku un noslēguma kadru. Kad
    kāds no tiem vai kāda nodaļa ir ierunājama, katram kadram tiek sintezēta
    SAVA ieruna, un kadrs ir tieši tik garš, cik tā runa. `voice` (viens fails
    pār visu lenti) paliek vecajiem ceļiem, kur nodaļu dalījuma nav.
    """
    out_dir = Path(out_dir or cards.CARDS_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    beats = plan_beats(title, sections or [], points,
                       cover_voice=cover_voice, end_voice=end_voice,
                       include_cover=include_cover, include_end=include_end,
                       max_points=max_points, frame_seconds=frame_seconds,
                       edge_seconds=edge_seconds, point_images=point_images)

    voices: list[str] = []
    spoken: dict[str, float] = {}   # ieruna -> sekundes, kadru garumam un atskaitei
    chosen: dict = {}               # kura balss un temps — atskaitei
    if voice is None and any(b["text"].strip() for b in beats):
        from app import tts as _tts

        if synth is None:
            synth = _tts.synthesize
        # ko sadaļa tiešām dabūja: balsi un tempu izšķir divi noteikumi, un
        # priekšskatījumā tas jāredz kā fakts, ne kā mans pieņēmums
        chosen = _tts.voice_choice(rules, section)
        # sadaļa iet līdzi: balsi un tempu var izvēlēties pa sadaļām
        # (izklaidei dzīvāka balss un ātrāks temps nekā pierobežas ziņai)
        voices = [synth(b["text"], section=section) if b["text"].strip() else ""
                  for b in beats]
        speech = [media_duration(v) if v else 0.0 for v in voices]
        spoken = {v: t for v, t in zip(voices, speech) if v}
        for beat, planned in zip(beats, plan_durations(
                [b["duration"] for b in beats], voices, speech)):
            beat["duration"] = planned
        for beat, path in zip(beats, voices):
            beat["voice"] = path
        dropped = _trim_beats(beats)
        if dropped:
            log.info("reel trimmed by %d chapter(s) to fit %ds",
                     dropped, VOICE_MAX_SECONDS)
        voices = [b.get("voice", "") for b in beats]

    # MI marķējums tikai tad, kad lentē TIEŠĀM ir sintezēta balss: tā ir
    # vienīgā daļa, kas ir mākslīgi ģenerēts medijs. Klusa lente ir foto un
    # teksts no žurnālista raksta, un zīmīte tur lasās kā apgalvojums, ka MI
    # ir uzrakstījis rakstu.
    from app import disclosure

    voiced = bool(voice or any(voices))
    mark_ai = disclosure.applies("reel", voiced, rules)

    total_frames = len(beats)
    docs = [_beat_html(b, i + 1, total_frames, section, title, image_url,
                       blur_image, cover_images, rules, mark_ai)
            for i, b in enumerate(beats)]
    durations = [b["duration"] for b in beats]

    out = out_dir / f"reel_{secrets.token_hex(6)}.mp4"
    with tempfile.TemporaryDirectory(dir=out_dir) as tmp:
        workdir = Path(tmp)
        frames = _render_frames(docs, workdir)
        total = _assemble(frames, workdir, out, durations=durations,
                          voice=voice, voices=voices or None)
    if report is not None:
        report.update({"voiced": voiced,
                       # cik ilgi tiešām SKAN balss (bez klusumiem un CTA
                       # kadra) — no tā redaktors var izrēķināt īsto tempu
                       # vārdos minūtē, nevis jāuzticas manam vērtējumam.
                       # Skaita PĒC apgriešanas: nomestas nodaļas ieruna
                       # lentē neskan, un tempu tā tikai sabojātu
                       "speech_seconds": round(
                           media_duration(voice) if voice
                           else sum(spoken.get(v, 0.0) for v in voices), 2),
                       # kura balss un kurš temps: izkomentēta sadaļas rinda
                       # izskatās gluži kā iestatījums, tāpēc rezultāts ir
                       # jāparāda, nevis jāliek redaktoram to izsecināt
                       "voice_used": chosen.get("voice", ""),
                       "voice_rate": chosen.get("rate"),
                       "voice_provider": chosen.get("provider", ""),
                       "voice_by_section": chosen.get("voice_by_section", False),
                       "rate_by_section": chosen.get("rate_by_section", False),
                       "frames": total_frames, "seconds": round(total, 2),
                       "narration": [b["text"] for b in beats if b["text"]]})
    log.info("reel built: %s (%d frames, %.0fs total%s)",
             out.name, total_frames, total, ", ar ierunu" if voiced else "")
    return str(out)
