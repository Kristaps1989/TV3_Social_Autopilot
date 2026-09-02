"""Threads adapteris: TEXT+saite, IMAGE, VIDEO ar statusa gaidīšanu,
CAROUSEL no bērniem, alt_text rezerve un atbilde zem ieraksta."""
import pytest

import adapters.threads as tmod
from adapters.base import PublishError
from adapters.threads import ThreadsAdapter


class FakeResp:
    def __init__(self, payload, code=200, text=""):
        self._payload, self.status_code, self.text = payload, code, text

    def json(self):
        return self._payload


def make_adapter(monkeypatch, posts, gets=None):
    """Adapteris ar viltus HTTP: `posts` saņem (path, data) un atgriež json;
    `gets` — statusa pieprasījumi (url, params)."""
    calls = {"post": [], "get": []}

    def fake_post(url, **kw):
        data = dict(kw.get("data") or {})
        data.pop("access_token", None)
        path = url.replace(tmod.API + "/", "")
        calls["post"].append((path, data))
        return posts(path, data)

    def fake_get(url, **kw):
        calls["get"].append((url, kw.get("params") or {}))
        return (gets or (lambda u, p: FakeResp({})))(url, kw.get("params") or {})

    monkeypatch.setattr(tmod.httpx, "post", fake_post)
    monkeypatch.setattr(tmod.httpx, "get", fake_get)
    monkeypatch.setattr(tmod.time, "sleep", lambda s: None)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.example")
    monkeypatch.setattr(tmod.credentials, "get", lambda key, session=None: "")
    adapter = ThreadsAdapter()
    adapter.user_id, adapter.token = "u1", "tok"
    return adapter, calls


def simple_posts(path, data):
    if path.endswith("/threads_publish"):
        return FakeResp({"id": "pub-" + data["creation_id"]})
    return FakeResp({"id": "c-" + data["media_type"].lower()})


def test_text_post_carries_link_attachment(monkeypatch):
    adapter, calls = make_adapter(monkeypatch, simple_posts)
    out = adapter.publish(text="Ziņa", link="https://tv3.lv/a?utm=1", images=[],
                          fmt="link")
    assert out == "pub-c-text"
    path, data = calls["post"][0]
    assert data["media_type"] == "TEXT"
    assert data["link_attachment"] == "https://tv3.lv/a?utm=1"


def test_photo_becomes_image_container_with_alt(monkeypatch):
    adapter, calls = make_adapter(monkeypatch, simple_posts)
    adapter.publish(text="Foto", link="", images=["data/cards/c1.png"], fmt="photo",
                    alt_text="Apraksts")
    path, data = calls["post"][0]
    assert data["media_type"] == "IMAGE"
    assert data["image_url"] == "https://app.example/media/c1.png"
    assert data["alt_text"] == "Apraksts"
    assert data["text"] == "Foto"
    assert calls["post"][1][0] == "u1/threads_publish"


def test_alt_text_rejection_retries_without_it(monkeypatch):
    def posts(path, data):
        if "alt_text" in data:
            return FakeResp({}, 400, "(#100) Invalid parameter alt_text")
        return simple_posts(path, data)

    adapter, calls = make_adapter(monkeypatch, posts)
    out = adapter.publish(text="Foto", link="", images=["x.png"], fmt="photo",
                          alt_text="Apraksts")
    assert out == "pub-c-image"
    assert "alt_text" in calls["post"][0][1]
    assert "alt_text" not in calls["post"][1][1]


def test_reel_uploads_video_and_waits_for_finished(monkeypatch):
    statuses = iter(["IN_PROGRESS", "FINISHED"])

    def gets(url, params):
        assert params["fields"] == "status,error_message"
        return FakeResp({"status": next(statuses)})

    adapter, calls = make_adapter(monkeypatch, simple_posts, gets)
    out = adapter.publish(text="Lente", link="", images=["data/reels/r.mp4"],
                          fmt="reel", alt_text="Alt")
    assert out == "pub-c-video"
    data = calls["post"][0][1]
    assert data["media_type"] == "VIDEO"
    assert data["video_url"] == "https://app.example/media/r.mp4"
    assert data["alt_text"] == "Alt"
    assert len(calls["get"]) == 2
    assert calls["get"][0][0] == f"{tmod.API}/c-video"
    # publicēšana tikai pēc FINISHED
    assert calls["post"][-1][0] == "u1/threads_publish"


def test_video_processing_error_is_not_retried(monkeypatch):
    gets = lambda u, p: FakeResp({"status": "ERROR", "error_message": "bad codec"})
    adapter, _ = make_adapter(monkeypatch, simple_posts, gets)
    with pytest.raises(PublishError) as e:
        adapter.publish(text="Lente", link="", images=["r.mp4"], fmt="reel")
    assert not e.value.retryable
    assert "bad codec" in str(e.value)


def test_video_processing_timeout_is_retryable(monkeypatch):
    clock = iter([0, 1, 1000, 1001, 1002])
    monkeypatch.setattr(tmod.time, "monotonic", lambda: next(clock))
    adapter, _ = make_adapter(monkeypatch, simple_posts,
                              lambda u, p: FakeResp({"status": "IN_PROGRESS"}))
    with pytest.raises(PublishError) as e:
        adapter.publish(text="Lente", link="", images=["r.mp4"], fmt="reel")
    assert e.value.retryable


def test_carousel_builds_children_then_parent(monkeypatch):
    n = iter(range(100))

    def posts(path, data):
        if path.endswith("/threads_publish"):
            return FakeResp({"id": "pub-1"})
        return FakeResp({"id": f"{data['media_type'].lower()}-{next(n)}"})

    adapter, calls = make_adapter(monkeypatch, posts,
                                  lambda u, p: FakeResp({"status": "FINISHED"}))
    out = adapter.publish(text="Kartītes", link="",
                          images=["a.png", "b.png", "c.png"], fmt="card_carousel",
                          card_links=["l1", "l2", "l3"], card_titles=["t1", "t2", "t3"],
                          alt_text="Alt")
    assert out == "pub-1"
    kids = [d for p, d in calls["post"] if d.get("is_carousel_item") == "true"]
    assert len(kids) == 3
    assert all(d["media_type"] == "IMAGE" and d["alt_text"] == "Alt" for d in kids)
    parent = next(d for p, d in calls["post"] if d.get("media_type") == "CAROUSEL")
    assert parent["children"] == "image-0,image-1,image-2"
    assert parent["text"] == "Kartītes"
    assert "alt_text" not in parent
    # karuseli gaida līdz FINISHED tāpat kā video
    assert calls["get"][0][0] == f"{tmod.API}/carousel-3"


def test_single_image_carousel_degrades_to_image(monkeypatch):
    adapter, calls = make_adapter(monkeypatch, simple_posts)
    adapter.publish(text="Viena", link="", images=["a.png"], fmt="card_carousel")
    assert calls["post"][0][1]["media_type"] == "IMAGE"


def test_media_without_public_url_falls_back_to_text(monkeypatch):
    adapter, calls = make_adapter(monkeypatch, simple_posts)
    monkeypatch.delenv("PUBLIC_BASE_URL")
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    adapter.publish(text="Bez", link="https://tv3.lv/b", images=["local.png"],
                    fmt="photo")
    data = calls["post"][0][1]
    assert data["media_type"] == "TEXT"
    assert data["link_attachment"] == "https://tv3.lv/b"


def test_comment_is_reply_to_post(monkeypatch):
    adapter, calls = make_adapter(monkeypatch, simple_posts)
    out = adapter.comment("post-77", "Saite: https://tv3.lv/x")
    assert out == "pub-c-text"
    data = calls["post"][0][1]
    assert data["media_type"] == "TEXT"
    assert data["reply_to_id"] == "post-77"
    assert data["text"] == "Saite: https://tv3.lv/x"
