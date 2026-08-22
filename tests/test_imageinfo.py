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
    fmt, _ = resolve_format(session, "fb_x", cfg, a, {"format": "link"})
    assert fmt == "photo"

    # landscape image keeps the link format the AI asked for
    monkeypatch.setattr(imageinfo, "probe_size", lambda url, **kw: (1200, 630))
    b = Article(guid="io-3", url="https://tv3.lv/io3", canonical_url="https://tv3.lv/io3",
                title="Horizontāla attēla raksts", section="news",
                images=["https://tv3.lv/wide.jpg"], raw_json={})
    session.add(b)
    session.flush()
    fmt, _ = resolve_format(session, "fb_x", cfg, b, {"format": "link"})
    assert fmt == "link"
