"""Threads via the official Threads API (two-step: create container, publish)."""
from __future__ import annotations

import time

import httpx

from adapters.base import Adapter, PublishError
from app import credentials

API = "https://graph.threads.net/v1.0"


class ThreadsAdapter(Adapter):
    platform = "threads"

    def __init__(self):
        self.user_id = credentials.get("threads_user_id")
        self.token = credentials.get("threads_token")

    def configured(self) -> bool:
        return bool(self.user_id and self.token)

    def _post(self, path: str, data: dict) -> dict:
        resp = httpx.post(f"{API}/{path}", data={**data, "access_token": self.token},
                          timeout=30)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise PublishError(f"Threads {resp.status_code}: {resp.text[:200]}", retryable=True)
        if resp.status_code >= 400:
            raise PublishError(f"Threads {resp.status_code}: {resp.text[:200]}", retryable=False)
        return resp.json()

    def publish(self, *, text: str, link: str, images: list[str], fmt: str) -> str:
        data: dict = {"text": text}
        if fmt == "photo" and images:
            data.update({"media_type": "IMAGE", "image_url": images[0]})
        else:
            data["media_type"] = "TEXT"
            if link:
                data["link_attachment"] = link
        container = self._post(f"{self.user_id}/threads", data)["id"]
        time.sleep(2)  # Threads recommends a short wait before publishing the container
        return self._post(f"{self.user_id}/threads_publish",
                          {"creation_id": container})["id"]
