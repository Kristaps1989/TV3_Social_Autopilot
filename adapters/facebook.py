"""Facebook Page via Meta Graph API.

Requires a long-lived Page token from a Business Manager System User
(FB_PAGE_ID, FB_PAGE_ACCESS_TOKEN). Permissions: pages_manage_posts,
pages_read_engagement.
"""
from __future__ import annotations

import httpx

from adapters.base import Adapter, PublishError
from app import credentials

GRAPH = "https://graph.facebook.com/v21.0"


class FacebookPageAdapter(Adapter):
    platform = "facebook_page"

    def __init__(self):
        self.page_id = credentials.get("fb_page_id")
        self.token = credentials.get("fb_page_token")

    def configured(self) -> bool:
        return bool(self.page_id and self.token)

    def _post(self, path: str, data: dict) -> dict:
        resp = httpx.post(f"{GRAPH}/{path}", data={**data, "access_token": self.token},
                          timeout=30)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise PublishError(f"FB {resp.status_code}: {resp.text[:200]}", retryable=True)
        if resp.status_code >= 400:
            raise PublishError(f"FB {resp.status_code}: {resp.text[:200]}", retryable=False)
        return resp.json()

    def publish(self, *, text: str, link: str, images: list[str], fmt: str) -> str:
        if fmt == "photo" and images:
            out = self._post(f"{self.page_id}/photos",
                             {"url": images[0], "caption": text})
            return out.get("post_id") or out.get("id", "")
        if fmt == "photo_album" and len(images) > 1:
            media_ids = []
            for img in images[:10]:
                out = self._post(f"{self.page_id}/photos",
                                 {"url": img, "published": "false"})
                media_ids.append(out["id"])
            data = {"message": text}
            for i, mid in enumerate(media_ids):
                data[f"attached_media[{i}]"] = f'{{"media_fbid":"{mid}"}}'
            return self._post(f"{self.page_id}/feed", data)["id"]
        data = {"message": text}
        if link:
            data["link"] = link
        return self._post(f"{self.page_id}/feed", data)["id"]

    def fetch_insights(self, platform_post_id: str) -> dict | None:
        try:
            resp = httpx.get(
                f"{GRAPH}/{platform_post_id}/insights",
                params={"metric": "post_impressions,post_clicks,post_reactions_like_total",
                        "access_token": self.token},
                timeout=30,
            )
            if resp.status_code != 200:
                return None
            values = {d["name"]: (d["values"][0]["value"] if d.get("values") else 0)
                      for d in resp.json().get("data", [])}
            return {
                "impressions": int(values.get("post_impressions", 0) or 0),
                "clicks": int(values.get("post_clicks", 0) or 0),
                "reactions": int(values.get("post_reactions_like_total", 0) or 0),
            }
        except httpx.HTTPError:
            return None
