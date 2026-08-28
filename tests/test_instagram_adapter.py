import httpx

from adapters import instagram
from adapters.base import public_image_url


class FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


def make_adapter(monkeypatch, calls):
    monkeypatch.setattr(instagram.credentials, "get",
                        lambda key, session=None: {"ig_user_id": "178414",
                                                   "fb_page_token": "tok"}.get(key, ""))

    def fake_post(url, data=None, timeout=None):
        calls.append((url, dict(data)))
        return FakeResponse({"id": f"c{len(calls)}"})

    monkeypatch.setattr(httpx, "post", fake_post)
    return instagram.InstagramAdapter()


def test_public_image_url(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://x.app/")
    assert public_image_url("https://cdn/img.png") == "https://cdn/img.png"
    assert public_image_url("/srv/data/cards/a.png") == "https://x.app/media/a.png"
    monkeypatch.delenv("PUBLIC_BASE_URL")
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    assert public_image_url("/srv/data/cards/a.png") == ""
    # Railway public domain works without any configuration
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "app.up.railway.app")
    assert public_image_url("/srv/data/cards/a.png") == "https://app.up.railway.app/media/a.png"


def test_photo_publish_uses_container_flow(monkeypatch):
    calls = []
    adapter = make_adapter(monkeypatch, calls)
    assert adapter.configured()
    out = adapter.publish(text="Sveiki", link="https://tv3.lv/a",
                          images=["https://cdn/img.png"], fmt="photo")
    assert out == "c2"
    assert calls[0][0].endswith("/178414/media")
    assert calls[0][1]["image_url"] == "https://cdn/img.png"
    assert calls[0][1]["caption"] == "Sveiki"
    assert calls[1][0].endswith("/178414/media_publish")
    assert calls[1][1]["creation_id"] == "c1"


def test_carousel_and_story_containers(monkeypatch):
    calls = []
    adapter = make_adapter(monkeypatch, calls)
    adapter.publish(text="T", link="", images=["https://cdn/1.png", "https://cdn/2.png"],
                    fmt="card_carousel")
    kinds = [d.get("media_type") for _, d in calls]
    assert kinds[:2] == [None, None]          # divi carousel bērni
    assert calls[0][1]["is_carousel_item"] == "true"
    assert calls[2][1]["media_type"] == "CAROUSEL"
    assert calls[2][1]["children"] == "c1,c2"

    calls.clear()
    adapter.publish(text="", link="", images=["https://cdn/1.png"], fmt="story")
    assert calls[0][1]["media_type"] == "STORIES"


def test_first_comment(monkeypatch):
    calls = []
    adapter = make_adapter(monkeypatch, calls)
    adapter.comment("mediaid", "https://tv3.lv/raksts")
    assert calls[0][0].endswith("/mediaid/comments")
    assert calls[0][1]["message"] == "https://tv3.lv/raksts"
