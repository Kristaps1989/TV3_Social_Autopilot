"""X (Twitter) API v2. Paid tier required — the adapter degrades gracefully
on 429 (queue retries with backoff, nothing is lost)."""
from __future__ import annotations

import os

import httpx

from adapters.base import Adapter, PublishError


class XAdapter(Adapter):
    platform = "x"

    def __init__(self):
        self.api_key = os.environ.get("X_API_KEY", "")
        self.api_secret = os.environ.get("X_API_SECRET", "")
        self.access_token = os.environ.get("X_ACCESS_TOKEN", "")
        self.access_secret = os.environ.get("X_ACCESS_TOKEN_SECRET", "")

    def configured(self) -> bool:
        return all([self.api_key, self.api_secret, self.access_token, self.access_secret])

    def _oauth1(self) -> httpx.Auth:
        try:
            from httpx_auth import OAuth1  # type: ignore

            return OAuth1(self.api_key, self.api_secret,
                          self.access_token, self.access_secret)
        except ImportError as e:
            raise PublishError(f"httpx_auth not installed: {e}", retryable=False) from e

    def publish(self, *, text: str, link: str, images: list[str], fmt: str) -> str:
        resp = httpx.post("https://api.twitter.com/2/tweets",
                          json={"text": text}, auth=self._oauth1(), timeout=30)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise PublishError(f"X {resp.status_code}: rate limited / server error",
                               retryable=True)
        if resp.status_code >= 400:
            raise PublishError(f"X {resp.status_code}: {resp.text[:200]}", retryable=False)
        return resp.json()["data"]["id"]
