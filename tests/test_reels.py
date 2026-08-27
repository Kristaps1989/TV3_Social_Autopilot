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

    def fake_build(title, section, image, points, out_dir=None):
        built.update(title=title, points=points)
        return "/data/cards/reel_x.mp4"

    monkeypatch.setattr(reels, "build_reel", fake_build)
    cfg = {"formats": ["photo", "reel"], "platform": "instagram"}
    fmt, media = pipeline.resolve_format(session, "ig", cfg, _article(session), {
        "format": "reel", "card_points": ["Pirmais āķis", "Otrais āķis"]})
    assert fmt == "reel"
    assert media == ["/data/cards/reel_x.mp4"]
    assert built["points"] == ["Pirmais āķis", "Otrais āķis"]

    # too few points -> falls back to a normal format
    fmt, media = pipeline.resolve_format(session, "ig", cfg, _article(session, "r-2"),
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
