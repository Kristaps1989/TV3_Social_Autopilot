import httpx
import pytest

from adapters import facebook


@pytest.fixture()
def adapter(monkeypatch):
    monkeypatch.setattr(facebook.credentials, "get",
                        lambda key, session=None: {"fb_page_id": "520",
                                                   "fb_page_token": "tok"}.get(key, ""))
    return facebook.FacebookPageAdapter()


class R:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._p = payload or {"id": "520_99"}
        self.text = str(self._p)

    def json(self):
        return self._p


def _capture(monkeypatch, responses=None):
    calls = []
    seq = list(responses or [])

    def fake_post(url, data=None, files=None, timeout=None):
        calls.append((url, dict(data or {}), files))
        return seq.pop(0) if seq else R()

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


CARDS = ["/data/cards/c0.png", "/data/cards/c1.png", "/data/cards/c2.png"]


def test_card_carousel_goes_out_as_swipeable_child_attachments(adapter, monkeypatch):
    import json

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.example")
    calls = _capture(monkeypatch)
    out = adapter.publish(text="Apraksts", link="https://tv3.lv/a?utm_content=9",
                          images=CARDS, fmt="card_carousel")
    assert out == "520_99"
    url, data, files = calls[0]
    assert url.endswith("/feed") and files is None
    cards = json.loads(data["child_attachments"])
    assert [c["picture"] for c in cards] == \
        [f"https://app.example/media/c{i}.png" for i in range(3)]
    assert all(c["link"] == "https://tv3.lv/a?utm_content=9" for c in cards)
    assert data["link"] == "https://tv3.lv/a?utm_content=9"
    # mūsu kārtība un mūsu CTA kartīte — bez FB pārkārtošanas/beigu kartītes
    assert data["multi_share_optimized"] == "false"
    assert data["multi_share_end_card"] == "false"


def test_carousel_keeps_the_cta_card_within_the_five_card_limit(adapter, monkeypatch):
    import json

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.example")
    calls = _capture(monkeypatch)
    imgs = [f"/data/cards/c{i}.png" for i in range(6)] + ["/data/cards/end.png"]
    adapter.publish(text="", link="https://tv3.lv/a", images=imgs, fmt="card_carousel")
    cards = json.loads(calls[0][1]["child_attachments"])
    names = [c["picture"].rsplit("/", 1)[-1] for c in cards]
    assert len(names) == 5
    assert names[0] == "c0.png" and names[-1] == "end.png"


def test_carousel_falls_back_to_album_without_public_base(adapter, monkeypatch, tmp_path):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    imgs = []
    for i in range(3):
        f = tmp_path / f"c{i}.png"
        f.write_bytes(b"png")
        imgs.append(str(f))
    calls = _capture(monkeypatch, [R(payload={"id": f"p{i}"}) for i in range(3)] + [R()])
    out = adapter.publish(text="Apraksts", link="https://tv3.lv/a",
                          images=imgs, fmt="card_carousel")
    assert out == "520_99"
    # 3 unpublished foto + /feed ar attached_media
    photo_calls = [c for c in calls if c[0].endswith("/photos")]
    assert len(photo_calls) == 3
    feed = calls[-1][1]
    assert "attached_media[0]" in feed and "child_attachments" not in feed


def test_carousel_rejected_by_fb_falls_back_to_album(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.example")
    imgs = []
    for i in range(2):
        f = tmp_path / f"c{i}.png"
        f.write_bytes(b"png")
        imgs.append(str(f))
    # pirmais /feed (karuselis) -> 400; tad 2 foto + /feed albums
    calls = _capture(monkeypatch, [R(400, {"error": "no carousel for you"}),
                                   R(payload={"id": "p0"}), R(payload={"id": "p1"}),
                                   R()])
    out = adapter.publish(text="", link="https://tv3.lv/a", images=imgs,
                          fmt="card_carousel")
    assert out == "520_99"
    assert "child_attachments" in calls[0][1]
    assert "attached_media[0]" in calls[-1][1]


def test_retryable_carousel_error_is_raised_not_swallowed(adapter, monkeypatch):
    from adapters.base import PublishError

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.example")
    _capture(monkeypatch, [R(500, {"error": "boom"})])
    with pytest.raises(PublishError) as exc:
        adapter.publish(text="", link="https://tv3.lv/a", images=CARDS,
                        fmt="card_carousel")
    assert exc.value.retryable is True
