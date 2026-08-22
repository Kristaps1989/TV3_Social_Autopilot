from adapters.x import XAdapter, oauth1_header


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
