import httpx
import pytest

from app.models import Article


def _article(session, guid="r-1"):
    a = Article(guid=guid, url=f"https://tv3.lv/{guid}",
                canonical_url=f"https://tv3.lv/{guid}",
                title="Skaidrojums par notikumu", section="news",
                images=["https://cdn/img.png"], raw_json={})
    session.add(a)
    session.flush()
    return a


def test_resolve_format_builds_reel(session, monkeypatch):
    from app import pipeline, reels

    monkeypatch.setattr(reels, "available", lambda: True)
    built = {}

    def fake_build(title, section, image, points, out_dir=None, voice=None):
        built.update(title=title, points=points, voice=voice)
        return "/data/cards/reel_x.mp4"

    monkeypatch.setattr(reels, "build_reel", fake_build)
    cfg = {"formats": ["photo", "reel"], "platform": "instagram"}
    fmt, media, _r = pipeline.resolve_format(session, "ig", cfg, _article(session), {
        "format": "reel", "card_points": ["Pirmais āķis", "Otrais āķis"]})
    assert fmt == "reel"
    assert media == ["/data/cards/reel_x.mp4"]
    assert built["points"] == ["Pirmais āķis", "Otrais āķis"]

    # too few points -> falls back to a normal format
    fmt, media, _r = pipeline.resolve_format(session, "ig", cfg, _article(session, "r-2"),
                                         {"format": "reel", "card_points": ["Viens"]})
    assert fmt != "reel"


def test_reel_not_offered_to_chooser():
    from app.formats import suitable_formats

    a = Article(guid="r-3", url="u", canonical_url="u", title="T", section="news",
                images=["https://cdn/i.png"], raw_json={})
    assert "reel" not in suitable_formats(a, ["photo", "reel", "link"])


def test_instagram_reel_flow(monkeypatch):
    from adapters import instagram

    monkeypatch.setattr(instagram.credentials, "get",
                        lambda key, session=None: {"ig_user_id": "178",
                                                   "fb_page_token": "tok"}.get(key, ""))
    calls = []

    class R:
        status_code = 200

        def __init__(self, payload):
            self._p = payload

        def json(self):
            return self._p

    def fake_post(url, data=None, timeout=None):
        calls.append((url, dict(data)))
        return R({"id": f"c{len(calls)}"})

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.example")
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get",
                        lambda url, params=None, timeout=None: R({"status_code": "FINISHED"}))
    out = instagram.InstagramAdapter().publish(
        text="Apraksts", link="", images=["/data/cards/reel_x.mp4"], fmt="reel")
    assert out == "c2"
    assert calls[0][1]["media_type"] == "REELS"
    assert calls[0][1]["video_url"].endswith("/media/reel_x.mp4")
    assert calls[1][0].endswith("/media_publish")


def test_facebook_reel_flow(monkeypatch, tmp_path):
    from adapters import facebook

    monkeypatch.setattr(facebook.credentials, "get",
                        lambda key, session=None: {"fb_page_id": "520",
                                                   "fb_page_token": "tok"}.get(key, ""))
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"mp4-bytes")
    graph_calls, uploads = [], []

    class R:
        def __init__(self, status, payload):
            self.status_code = status
            self._p = payload
            self.text = str(payload)

        def json(self):
            return self._p

    def fake_post(url, data=None, files=None, content=None, headers=None, timeout=None):
        if "rupload" in url:
            uploads.append((url, content, dict(headers or {})))
            return R(200, {"success": True})
        graph_calls.append((url, dict(data or {})))
        if data.get("upload_phase") == "start":
            return R(200, {"video_id": "v9",
                           "upload_url": "https://rupload.facebook.com/video-upload/v21.0/v9"})
        return R(200, {"success": True})

    monkeypatch.setattr(httpx, "post", fake_post)
    out = facebook.FacebookPageAdapter().publish(
        text="Apraksts", link="", images=[str(video)], fmt="reel")
    assert out == "v9"
    assert uploads[0][1] == b"mp4-bytes"
    assert uploads[0][2]["offset"] == "0"
    assert graph_calls[-1][1]["upload_phase"] == "finish"
    assert graph_calls[-1][1]["video_state"] == "PUBLISHED"


def test_article_video_extraction():
    from app.reels import article_video

    a = Article(guid="v-1", url="u", canonical_url="u", title="T", section="news",
                raw_json={"video_url": "https://cdn/klips.mp4"})
    assert article_video(a) == "https://cdn/klips.mp4"
    b = Article(guid="v-2", url="u", canonical_url="u", title="T", section="news",
                raw_json={"video": {"url": "https://cdn/k2.mp4"}})
    assert article_video(b) == "https://cdn/k2.mp4"
    c = Article(guid="v-3", url="u", canonical_url="u", title="T", section="news",
                raw_json={"video": "ne-url"})
    assert article_video(c) == ""


def test_resolve_format_prefers_real_video(session, monkeypatch):
    from app import pipeline, reels

    monkeypatch.setattr(reels, "available", lambda: True)
    monkeypatch.setattr(reels, "build_video_reel",
                        lambda url, out_dir=None: "/data/cards/reel_v.mp4")
    monkeypatch.setattr(reels, "build_reel",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError(
                            "slideshow nedrīkst būvēties, ja ir īsts video")))
    a = _article(session, "v-4")
    a.raw_json = {"video_url": "https://cdn/klips.mp4"}
    cfg = {"formats": ["photo", "reel"], "platform": "instagram"}
    fmt, media, _r = pipeline.resolve_format(session, "ig", cfg, a,
                                         {"format": "reel", "card_points": []})
    assert fmt == "reel"
    assert media == ["/data/cards/reel_v.mp4"]


def test_upsert_refreshes_video_but_keeps_caches(session):
    from app.ingest import upsert_article

    base = dict(guid="v-5", url="https://tv3.lv/v5", canonical_url="https://tv3.lv/v5",
                title="T", lead="L", categories=[], term_ids=[], images=[],
                published_at=None, editor_status="can", editor_timeframe="",
                section="news", feed_name="f", raw_json={})
    article, created, _ = upsert_article(session, dict(base))
    assert created
    article.raw_json = {"_img_wh": [100, 200]}
    session.flush()
    updated = dict(base, raw_json={"video_url": "https://cdn/k.mp4"})
    article, created, _ = upsert_article(session, updated)
    assert not created
    assert article.raw_json["video_url"] == "https://cdn/k.mp4"
    assert article.raw_json["_img_wh"] == [100, 200]


def _synthetic_video(path, ffmpeg, with_audio=True):
    import subprocess

    args = [ffmpeg, "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=3:size=540x960:rate=25"]
    if with_audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:duration=3"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if with_audio:
        args += ["-c:a", "aac", "-shortest"]
    subprocess.run(args + [str(path)], check=True, capture_output=True)


def test_build_video_reel_end_to_end(monkeypatch, tmp_path):
    import os

    from app import reels

    try:
        import imageio_ffmpeg

        monkeypatch.setenv("FFMPEG_BIN", imageio_ffmpeg.get_ffmpeg_exe())
    except ImportError:
        pass
    if not os.environ.get("PLAYWRIGHT_CHROMIUM") and os.path.exists("/opt/pw-browsers/chromium"):
        monkeypatch.setenv("PLAYWRIGHT_CHROMIUM", "/opt/pw-browsers/chromium")
    if not reels.available():
        pytest.skip("ffmpeg or Chromium unavailable")
    src = tmp_path / "src.mp4"
    _synthetic_video(src, reels.ffmpeg_bin(), with_audio=True)
    out = reels.build_video_reel(str(src), out_dir=tmp_path)
    path = tmp_path / out.split("/")[-1]
    assert path.exists() and path.stat().st_size > 10000
    # klusa avota video arī iziet cauri (CTA kadram vajag saskanīgu audio)
    src2 = tmp_path / "src2.mp4"
    _synthetic_video(src2, reels.ffmpeg_bin(), with_audio=False)
    out2 = reels.build_video_reel(str(src2), out_dir=tmp_path)
    assert (tmp_path / out2.split("/")[-1]).exists()


def test_build_reel_end_to_end(monkeypatch, tmp_path):
    import os

    from app import reels

    try:
        import imageio_ffmpeg

        monkeypatch.setenv("FFMPEG_BIN", imageio_ffmpeg.get_ffmpeg_exe())
    except ImportError:
        pass
    if not os.environ.get("PLAYWRIGHT_CHROMIUM") and os.path.exists("/opt/pw-browsers/chromium"):
        monkeypatch.setenv("PLAYWRIGHT_CHROMIUM", "/opt/pw-browsers/chromium")
    if not reels.available():
        pytest.skip("ffmpeg or Chromium unavailable")
    out = reels.build_reel("Traģēdija Nepālā kļūst arvien lielāka", "news", "",
                           ["Pazudušo saraksts aug", "Latvieši starp meklētajiem"],
                           out_dir=tmp_path)
    path = tmp_path / out.split("/")[-1]
    assert path.exists() and path.stat().st_size > 10000
    assert path.suffix == ".mp4"


# --- ieruna (voice-over) ---------------------------------------------------

def _synthetic_voice(path, ffmpeg, seconds=12):
    """Runas vietā sinuss — mums svarīgs ir garums, nevis saturs."""
    import subprocess

    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"sine=frequency=220:duration={seconds}",
                    "-c:a", "aac", str(path)], check=True, capture_output=True)


def test_voice_script_prepares_text_for_speaking():
    from app import reels

    raw = ("Rīgā (Bauskas ielā) notika sprādziens. Vairāk lasi "
           "https://tv3.lv/zinas/kas-notika — tur ir viss stāsts par namu.")
    script = reels.voice_script(raw)
    assert "https://" not in script and "(" not in script
    assert "Bauskas ielā notika sprādziens." in script
    assert "  " not in script


def test_voice_script_cuts_at_a_sentence():
    from app import reels

    long_text = "Šis ir viens pilns teikums ar faktiem. " * 12
    script = reels.voice_script(long_text, max_words=20)
    assert script.endswith(".")
    assert len(script.split()) <= 20


def test_voice_script_rejects_a_stub():
    from app import reels

    assert reels.voice_script("Par īsu.") == ""
    assert reels.voice_script("") == ""


def test_frames_stretch_to_the_narration():
    from app import reels

    # 3 kadri x 2.8 s = 8.4 s video pret 14 s runu -> kadri aug proporcionāli
    stretched = reels._stretch_to_voice([2.8, 2.8, 2.8], 14.0)
    assert sum(stretched) == pytest.approx(14.0 + reels.VOICE_TAIL_SECONDS)
    assert stretched[0] == pytest.approx(stretched[2])   # CTA nestāv viens
    # īsa ieruna kadrus nesaīsina — teksts kadrā tāpat jāpaspēj izlasīt
    assert reels._stretch_to_voice([2.8, 2.8], 3.0) == [2.8, 2.8]
    # un neaug bez gala
    assert sum(reels._stretch_to_voice([2.8, 2.8], 300.0)) <= reels.VOICE_MAX_SECONDS


def test_build_reel_with_voice_end_to_end(monkeypatch, tmp_path):
    import os

    from app import reels

    try:
        import imageio_ffmpeg

        monkeypatch.setenv("FFMPEG_BIN", imageio_ffmpeg.get_ffmpeg_exe())
    except ImportError:
        pass
    if not os.environ.get("PLAYWRIGHT_CHROMIUM") and os.path.exists("/opt/pw-browsers/chromium"):
        monkeypatch.setenv("PLAYWRIGHT_CHROMIUM", "/opt/pw-browsers/chromium")
    if not reels.available():
        pytest.skip("ffmpeg or Chromium unavailable")

    voice = tmp_path / "voice.m4a"
    _synthetic_voice(voice, reels.ffmpeg_bin(), seconds=12)
    assert reels.media_duration(voice) == pytest.approx(12, abs=0.5)

    out = reels.build_reel("Kas zināms par Bauskas ielas namu", "news", "",
                           ["Jumts daļēji iebruka", "Lēmums vēl nav pieņemts"],
                           out_dir=tmp_path, voice=voice)
    path = tmp_path / out.split("/")[-1]
    assert path.exists() and path.stat().st_size > 10000
    # video ir izstiepts, lai ieruna izskanētu līdz galam
    assert reels.media_duration(path) >= 12
