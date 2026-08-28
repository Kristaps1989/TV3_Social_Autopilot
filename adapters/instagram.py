"""Instagram Business via the Graph API content publishing flow.

Reuses the Facebook Page connection: the IG Business account must be
linked to the connected page, and the page token (with instagram_basic +
instagram_content_publish in the login configuration) authorizes
publishing. Two-step: create a media container, then publish it. Images
must be public URLs — locally rendered files are exposed through the
app's /media endpoint (PUBLIC_BASE_URL).
"""
from __future__ import annotations

import httpx

from adapters.base import Adapter, PublishError, is_video, public_image_url
from app import credentials

GRAPH = "https://graph.facebook.com/v21.0"


class InstagramAdapter(Adapter):
    platform = "instagram"

    def __init__(self):
        self.user_id = credentials.get("ig_user_id")
        self.token = credentials.get("fb_page_token")

    def configured(self) -> bool:
        return bool(self.user_id and self.token)

    def _post(self, path: str, data: dict) -> dict:
        resp = httpx.post(f"{GRAPH}/{path}", data={**data, "access_token": self.token},
                          timeout=60)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise PublishError(f"IG {resp.status_code}: {resp.text[:200]}", retryable=True)
        if resp.status_code >= 400:
            raise PublishError(f"IG {resp.status_code}: {resp.text[:200]}", retryable=False)
        return resp.json()

    def _container(self, data: dict) -> str:
        return self._post(f"{self.user_id}/media", data)["id"]

    def publish(self, *, text: str, link: str, images: list[str], fmt: str) -> str:
        urls = [u for u in (public_image_url(i) for i in images) if u]
        if not urls:
            raise PublishError(
                "Instagram vajag publisku attēla URL — nav attēla vai nav "
                "uzstādīts PUBLIC_BASE_URL", retryable=False)
        if fmt == "reel":
            container = self._container({"media_type": "REELS",
                                         "video_url": urls[0],
                                         "caption": text,
                                         "share_to_feed": "true"})
            self._wait_processed(container)
        elif fmt == "story":
            if is_video(urls[0]):
                container = self._container({"media_type": "STORIES",
                                             "video_url": urls[0]})
                self._wait_processed(container)
            else:
                container = self._container({"media_type": "STORIES",
                                             "image_url": urls[0]})
        elif fmt in ("photo_album", "card_carousel") and len(urls) > 1:
            children = [self._container({"image_url": u, "is_carousel_item": "true"})
                        for u in urls[:10]]
            container = self._container({"media_type": "CAROUSEL",
                                         "children": ",".join(children),
                                         "caption": text})
        else:
            container = self._container({"image_url": urls[0], "caption": text})
        return self._post(f"{self.user_id}/media_publish",
                          {"creation_id": container})["id"]

    def _wait_processed(self, container_id: str, timeout: int = 240) -> None:
        """Video containers process asynchronously; publishing before the
        status is FINISHED fails, so poll until ready."""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r = httpx.get(f"{GRAPH}/{container_id}", timeout=30,
                          params={"fields": "status_code",
                                  "access_token": self.token})
            status = r.json().get("status_code") if r.status_code == 200 else None
            if status == "FINISHED":
                return
            if status == "ERROR":
                raise PublishError("IG video apstrāde neizdevās (status ERROR)",
                                   retryable=False)
            time.sleep(5)
        raise PublishError("IG video apstrāde pārsniedza laika limitu",
                           retryable=True)

    def comment(self, post_id: str, message: str) -> str:
        """Caption links are not clickable on IG, so the article link lands
        as the first comment (same tactic as Facebook photo posts)."""
        return self._post(f"{post_id}/comments", {"message": message}).get("id", "")

    def fetch_insights(self, platform_post_id: str) -> dict | None:
        try:
            resp = httpx.get(
                f"{GRAPH}/{platform_post_id}/insights",
                params={"metric": "reach,likes", "access_token": self.token},
                timeout=30,
            )
            if resp.status_code != 200:
                return None
            values = {d["name"]: (d["values"][0]["value"] if d.get("values") else 0)
                      for d in resp.json().get("data", [])}
            # organic IG offers no click metric; GA4 utm data covers clicks
            return {
                "impressions": int(values.get("reach", 0) or 0),
                "clicks": 0,
                "reactions": int(values.get("likes", 0) or 0),
            }
        except httpx.HTTPError:
            return None
