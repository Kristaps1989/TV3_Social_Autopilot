"""X (Twitter) API v2 with native OAuth 1.0a signing (no extra deps).

Paid tier required. Supports text/link tweets and photo tweets (media is
uploaded via the v1.1 media/upload endpoint, then attached to the v2
tweet). Since X link cards no longer show the headline, photo posts with
the branded title image + link in text are the best-practice format for
visual stories. Degrades gracefully on 429 (queue retries with backoff).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets as pysecrets
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from adapters.base import Adapter, PublishError

TWEETS_URL = "https://api.twitter.com/2/tweets"
UPLOAD_URL = "https://upload.twitter.com/1.1/media/upload.json"


def oauth1_header(method: str, url: str, *, consumer_key: str, consumer_secret: str,
                  token: str, token_secret: str, extra_params: dict | None = None,
                  nonce: str | None = None, timestamp: str | None = None) -> str:
    """RFC 5849 HMAC-SHA1 signature. extra_params must include query/body
    form params (NOT JSON bodies or multipart payloads, per the spec)."""
    oauth = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce or pysecrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp or str(int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    enc = lambda s: quote(str(s), safe="")  # noqa: E731
    all_params = {**oauth, **(extra_params or {})}
    param_str = "&".join(f"{enc(k)}={enc(v)}" for k, v in sorted(all_params.items()))
    base = "&".join([method.upper(), enc(url), enc(param_str)])
    key = f"{enc(consumer_secret)}&{enc(token_secret)}"
    signature = base64.b64encode(
        hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    oauth["oauth_signature"] = signature
    return "OAuth " + ", ".join(f'{enc(k)}="{enc(v)}"' for k, v in sorted(oauth.items()))


class XAdapter(Adapter):
    platform = "x"

    def __init__(self):
        from app import credentials

        self.api_key = credentials.get("x_api_key")
        self.api_secret = credentials.get("x_api_secret")
        self.access_token = credentials.get("x_access_token")
        self.access_secret = credentials.get("x_access_secret")

    def configured(self) -> bool:
        return all([self.api_key, self.api_secret, self.access_token, self.access_secret])

    def _auth(self, method: str, url: str, extra_params: dict | None = None) -> dict:
        return {"Authorization": oauth1_header(
            method, url, consumer_key=self.api_key, consumer_secret=self.api_secret,
            token=self.access_token, token_secret=self.access_secret,
            extra_params=extra_params)}

    @staticmethod
    def _check(resp: httpx.Response, what: str) -> None:
        if resp.status_code == 429 or resp.status_code >= 500:
            raise PublishError(f"X {what} {resp.status_code}: rate limited / server error",
                               retryable=True)
        if resp.status_code >= 400:
            raise PublishError(f"X {what} {resp.status_code}: {resp.text[:200]}",
                               retryable=False)

    def _upload_media(self, image: str) -> str:
        if image.startswith("http"):
            r = httpx.get(image, timeout=30, follow_redirects=True)
            r.raise_for_status()
            payload = r.content
        else:
            payload = Path(image).read_bytes()
        resp = httpx.post(UPLOAD_URL, headers=self._auth("POST", UPLOAD_URL),
                          files={"media": payload}, timeout=60)
        self._check(resp, "media upload")
        return resp.json()["media_id_string"]

    def publish(self, *, text: str, link: str, images: list[str], fmt: str) -> str:
        payload: dict = {"text": text}
        if fmt in ("photo", "story") and images:
            try:
                payload["media"] = {"media_ids": [self._upload_media(images[0])]}
            except (PublishError, httpx.HTTPError, OSError):
                pass  # image failed -> still worth posting the text+link tweet
        resp = httpx.post(TWEETS_URL, json=payload,
                          headers=self._auth("POST", TWEETS_URL), timeout=30)
        self._check(resp, "tweet")
        return resp.json()["data"]["id"]
