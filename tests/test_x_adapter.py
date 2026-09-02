import pytest

from adapters.x import XAdapter, oauth1_header


@pytest.fixture(autouse=True)
def _no_credential_store(monkeypatch):
    """Adapteris atslēgas lasa no DB; te tās liek tests pats."""
    from app import credentials

    monkeypatch.setattr(credentials, "get", lambda key, session=None: "")


def test_oauth1_signature_matches_twitter_reference_vector():
    """The worked example from X's own 'Creating a signature' docs."""
    header = oauth1_header(
        "POST", "https://api.twitter.com/1.1/statuses/update.json",
        consumer_key="xvz1evFS4wEEPTGEFPHBog",
        consumer_secret="kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
        token="370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
        token_secret="LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
        extra_params={
            "include_entities": "true",
            "status": "Hello Ladies + Gentlemen, a signed OAuth request!",
        },
        nonce="kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg",
        timestamp="1318622958",
    )
    assert 'oauth_signature="hCtSmYh%2BiHYCEqBWrE7C7hYmtUk%3D"' in header
    assert header.startswith("OAuth ")
    assert 'oauth_consumer_key="xvz1evFS4wEEPTGEFPHBog"' in header


def test_x_photo_publish_uploads_then_tweets(monkeypatch):
    calls = []

    class FakeResp:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload
            self.text = ""

        def json(self):
            return self._payload

    def fake_post(url, **kw):
        calls.append((url, kw))
        if "upload.twitter.com" in url:
            return FakeResp({"media_id_string": "m123"})
        return FakeResp({"data": {"id": "tweet-9"}})

    import adapters.x as xmod

    monkeypatch.setattr(xmod.httpx, "post", fake_post)
    adapter = XAdapter()
    adapter.api_key, adapter.api_secret = "k", "s"
    adapter.access_token, adapter.access_secret = "t", "ts"

    import tempfile, os

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(b"png-bytes")
        path = f.name
    try:
        tweet_id = adapter.publish(text="Ziņa https://tv3.lv/a", link="",
                                   images=[path], fmt="photo")
    finally:
        os.unlink(path)

    assert tweet_id == "tweet-9"
    assert "upload.twitter.com" in calls[0][0]
    assert calls[1][1]["json"]["media"]["media_ids"] == ["m123"]
    assert calls[1][1]["headers"]["Authorization"].startswith("OAuth ")


def test_x_photo_falls_back_to_text_when_upload_fails(monkeypatch):
    class FakeResp:
        def __init__(self, code, payload=None):
            self.status_code = code
            self._payload = payload or {}
            self.text = "err"

        def json(self):
            return self._payload

    def fake_post(url, **kw):
        if "upload.twitter.com" in url:
            return FakeResp(400)
        return FakeResp(200, {"data": {"id": "tweet-10"}})

    import adapters.x as xmod

    monkeypatch.setattr(xmod.httpx, "post", fake_post)
    adapter = XAdapter()
    adapter.api_key, adapter.api_secret = "k", "s"
    adapter.access_token, adapter.access_secret = "t", "ts"
    tweet_id = adapter.publish(text="Ziņa", link="", images=["/nonexistent.png"],
                               fmt="photo")
    assert tweet_id == "tweet-10"  # tweet still goes out without the image


class _Resp:
    def __init__(self, payload=None, code=200):
        self.status_code = code
        self._payload = payload or {}
        self.text = "" if code < 400 else "err"
        self.content = b"{}" if payload is not None else b""

    def json(self):
        return self._payload


def _adapter(monkeypatch, fake_post, fake_get=None):
    import adapters.x as xmod

    monkeypatch.setattr(xmod.httpx, "post", fake_post)
    if fake_get is not None:
        monkeypatch.setattr(xmod.httpx, "get", fake_get)
    monkeypatch.setattr(xmod.time, "sleep", lambda s: None)
    adapter = XAdapter()
    adapter.api_key, adapter.api_secret = "k", "s"
    adapter.access_token, adapter.access_secret = "t", "ts"
    return adapter


def test_x_reel_uses_chunked_upload_and_waits_for_processing(monkeypatch, tmp_path):
    import adapters.x as xmod

    monkeypatch.setattr(xmod, "CHUNK_BYTES", 4)
    video = tmp_path / "r.mp4"
    video.write_bytes(b"0123456789")  # 10 baiti -> 3 gabali pa 4
    calls, gets = [], []

    def fake_post(url, **kw):
        data = kw.get("data") or {}
        calls.append((url, data, kw))
        if url == xmod.UPLOAD_URL:
            if data.get("command") == "INIT":
                assert data["media_category"] == "tweet_video"
                assert data["total_bytes"] == "10"
                return _Resp({"media_id_string": "vid-1"})
            if data.get("command") == "APPEND":
                assert "media" in kw["files"]
                return _Resp(None, 204)
            if data.get("command") == "FINALIZE":
                return _Resp({"media_id_string": "vid-1",
                              "processing_info": {"state": "pending",
                                                  "check_after_secs": 1}})
        return _Resp({"data": {"id": "tweet-v"}})

    states = iter([{"state": "in_progress", "check_after_secs": 1},
                   {"state": "succeeded"}])

    def fake_get(url, **kw):
        gets.append(kw.get("params"))
        return _Resp({"processing_info": next(states)})

    adapter = _adapter(monkeypatch, fake_post, fake_get)
    tweet_id = adapter.publish(text="Lente", link="", images=[str(video)], fmt="reel")
    assert tweet_id == "tweet-v"
    commands = [c[1].get("command") for c in calls if c[0] == xmod.UPLOAD_URL]
    assert commands == ["INIT", "APPEND", "APPEND", "APPEND", "FINALIZE"]
    appends = [c for c in calls if c[1].get("command") == "APPEND"]
    assert [a[1]["segment_index"] for a in appends] == ["0", "1", "2"]
    assert b"".join(a[2]["files"]["media"] for a in appends) == b"0123456789"
    # INIT/FINALIZE formas lauki ir parakstā; APPEND multipart — ne
    assert 'oauth_signature=' in calls[0][2]["headers"]["Authorization"]
    assert [g["command"] for g in gets] == ["STATUS", "STATUS"]
    assert calls[-1][2]["json"]["media"]["media_ids"] == ["vid-1"]


def test_x_video_processing_failure_posts_text_only(monkeypatch, tmp_path):
    import adapters.x as xmod

    video = tmp_path / "r.mp4"
    video.write_bytes(b"abc")
    tweets = []

    def fake_post(url, **kw):
        data = kw.get("data") or {}
        if url == xmod.UPLOAD_URL:
            if data.get("command") == "FINALIZE":
                return _Resp({"processing_info": {"state": "failed",
                                                  "error": {"message": "InvalidMedia"}}})
            return _Resp({"media_id_string": "vid-2"})
        tweets.append(kw["json"])
        return _Resp({"data": {"id": "tweet-t"}})

    adapter = _adapter(monkeypatch, fake_post)
    assert adapter.publish(text="Lente https://tv3.lv/r", link="",
                           images=[str(video)], fmt="reel") == "tweet-t"
    assert tweets == [{"text": "Lente https://tv3.lv/r"}]


def test_x_card_carousel_is_a_four_image_tweet(monkeypatch, tmp_path):
    import adapters.x as xmod

    paths = []
    for i in range(6):
        p = tmp_path / f"c{i}.png"
        p.write_bytes(b"png")
        paths.append(str(p))
    uploads, alts, tweets = [], [], []

    def fake_post(url, **kw):
        if url == xmod.UPLOAD_URL:
            uploads.append(kw["files"]["media"])
            return _Resp({"media_id_string": f"m{len(uploads)}"})
        if url == xmod.MEDIA_META_URL:
            alts.append(kw["json"])
            return _Resp({})
        tweets.append(kw["json"])
        return _Resp({"data": {"id": "tweet-c"}})

    adapter = _adapter(monkeypatch, fake_post)
    assert adapter.publish(text="Kartītes", link="", images=paths, fmt="card_carousel",
                           card_links=["l"] * 6, card_titles=["t"] * 6,
                           alt_text="Alt") == "tweet-c"
    assert len(uploads) == xmod.MAX_IMAGES == 4
    assert tweets[0]["media"]["media_ids"] == ["m1", "m2", "m3", "m4"]
    assert [a["media_id"] for a in alts] == ["m1", "m2", "m3", "m4"]
    assert alts[0]["alt_text"]["text"] == "Alt"


def test_x_comment_is_a_reply_in_thread(monkeypatch):
    import adapters.x as xmod

    posted = []

    def fake_post(url, **kw):
        posted.append((url, kw["json"]))
        return _Resp({"data": {"id": "reply-1"}})

    adapter = _adapter(monkeypatch, fake_post)
    assert adapter.comment("tweet-9", "Saite: https://tv3.lv/a") == "reply-1"
    assert posted[0][0] == xmod.TWEETS_URL
    assert posted[0][1] == {"text": "Saite: https://tv3.lv/a",
                            "reply": {"in_reply_to_tweet_id": "tweet-9"}}
