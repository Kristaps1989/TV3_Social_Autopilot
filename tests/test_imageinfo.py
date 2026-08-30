import struct

from app import imageinfo
from app.models import Article


def png_header(w, h):
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
            + struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00")


def jpeg_header(w, h):
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    sof0 = b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", h, w)
    return b"\xff\xd8" + app0 + sof0 + b"\x00" * 20


def gif_header(w, h):
    return b"GIF89a" + struct.pack("<HH", w, h) + b"\x00" * 10


def test_parse_png_jpeg_gif():
    assert imageinfo.parse_dimensions(png_header(1200, 630)) == (1200, 630)
    assert imageinfo.parse_dimensions(jpeg_header(1080, 1350)) == (1080, 1350)
    assert imageinfo.parse_dimensions(gif_header(300, 200)) == (300, 200)
    assert imageinfo.parse_dimensions(b"not an image at all") is None


def test_orientation_cached_on_article(monkeypatch):
    calls = []

    def fake_probe(url, **kw):
        calls.append(url)
        return (1080, 1920)

    monkeypatch.setattr(imageinfo, "probe_size", fake_probe)
    a = Article(guid="io-1", url="https://tv3.lv/io", canonical_url="https://tv3.lv/io",
                title="T", section="news", images=["https://tv3.lv/vert.jpg"],
                raw_json={})
    assert imageinfo.orientation(a) == "portrait"
    assert imageinfo.orientation(a) == "portrait"
    assert len(calls) == 1  # second call served from the cache
    assert a.raw_json["_img_wh"] == [1080, 1920]


def test_portrait_og_image_switches_link_to_photo(session, monkeypatch):
    from app.pipeline import resolve_format

    monkeypatch.setattr(imageinfo, "probe_size", lambda url, **kw: (1080, 1920))
    a = Article(guid="io-2", url="https://tv3.lv/io2", canonical_url="https://tv3.lv/io2",
                title="Vertikāla attēla raksts", section="news",
                images=["https://tv3.lv/vert.jpg"], raw_json={})
    session.add(a)
    session.flush()
    cfg = {"formats": ["link", "photo"], "platform": "facebook_page"}
    fmt, _, _r = resolve_format(session, "fb_x", cfg, a, {"format": "link"})
    assert fmt == "photo"

    # landscape image keeps the link format the AI asked for
    monkeypatch.setattr(imageinfo, "probe_size", lambda url, **kw: (1200, 630))
    b = Article(guid="io-3", url="https://tv3.lv/io3", canonical_url="https://tv3.lv/io3",
                title="Horizontāla attēla raksts", section="news",
                images=["https://tv3.lv/wide.jpg"], raw_json={})
    session.add(b)
    session.flush()
    fmt, _, _r = resolve_format(session, "fb_x", cfg, b, {"format": "link"})
    assert fmt == "link"


def test_photo_base_image_prefers_landscape(monkeypatch):
    from app.pipeline import photo_base_image

    sizes = {"https://tv3.lv/vert.jpg": (1080, 1920),
             "https://tv3.lv/wide.jpg": (1200, 630)}

    monkeypatch.setattr(imageinfo, "probe_size", lambda url, **kw: sizes.get(url))
    a = Article(guid="io-4", url="https://tv3.lv/io4", canonical_url="https://tv3.lv/io4",
                title="T", section="news",
                images=["https://tv3.lv/vert.jpg", "https://tv3.lv/wide.jpg"],
                raw_json={})
    assert photo_base_image(a) == "https://tv3.lv/wide.jpg"
    # per-URL sizes cached on the article
    assert a.raw_json["_img_dims"]["https://tv3.lv/vert.jpg"] == [1080, 1920]

    # no landscape alternative -> keep the chosen image
    b = Article(guid="io-5", url="https://tv3.lv/io5", canonical_url="https://tv3.lv/io5",
                title="T", section="news",
                images=["https://tv3.lv/vert.jpg"], raw_json={})
    assert photo_base_image(b) == "https://tv3.lv/vert.jpg"

    # landscape image chosen by the AI stays as-is
    c = Article(guid="io-6", url="https://tv3.lv/io6", canonical_url="https://tv3.lv/io6",
                title="T", section="news",
                images=["https://tv3.lv/wide.jpg", "https://tv3.lv/vert.jpg"],
                raw_json={})
    assert photo_base_image(c) == "https://tv3.lv/wide.jpg"


def test_photo_base_image_toggle_off(monkeypatch):
    from app import config
    from app.pipeline import photo_base_image

    monkeypatch.setattr(config, "load_rules",
                        lambda: {"photo_prefer_landscape": False})
    monkeypatch.setattr(imageinfo, "probe_size", lambda url, **kw: (1080, 1920))
    a = Article(guid="io-7", url="https://tv3.lv/io7", canonical_url="https://tv3.lv/io7",
                title="T", section="news",
                images=["https://tv3.lv/vert.jpg", "https://tv3.lv/wide.jpg"],
                raw_json={})
    assert photo_base_image(a) == "https://tv3.lv/vert.jpg"


def test_ensure_editable_dirs_seeds_volume(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "RULES_DIR", tmp_path / "rules")
    monkeypatch.setattr(config, "PROMPTS_DIR", tmp_path / "prompts")
    config.ensure_editable_dirs()
    assert (tmp_path / "rules" / "rules.yaml").exists()
    assert (tmp_path / "prompts" / "system_base.md").exists()
    # user edits are never overwritten
    (tmp_path / "rules" / "rules.yaml").write_text("edited: true", encoding="utf-8")
    config.ensure_editable_dirs()
    assert (tmp_path / "rules" / "rules.yaml").read_text(encoding="utf-8") == "edited: true"
