"""Facebook Page via Meta Graph API.

Requires a long-lived Page token from a Business Manager System User
(FB_PAGE_ID, FB_PAGE_ACCESS_TOKEN). Permissions: pages_manage_posts,
pages_read_engagement.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

from adapters.base import Adapter, PublishError, is_video, public_image_url
from app import credentials

GRAPH = "https://graph.facebook.com/v21.0"

log = logging.getLogger(__name__)


class FacebookPageAdapter(Adapter):
    platform = "facebook_page"

    def __init__(self):
        self.page_id = credentials.get("fb_page_id")
        self.token = credentials.get("fb_page_token")

    def configured(self) -> bool:
        return bool(self.page_id and self.token)

    def _post(self, path: str, data: dict, files: dict | None = None) -> dict:
        resp = httpx.post(f"{GRAPH}/{path}", data={**data, "access_token": self.token},
                          files=files, timeout=60)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise PublishError(f"FB {resp.status_code}: {resp.text[:200]}", retryable=True)
        if resp.status_code >= 400:
            raise PublishError(f"FB {resp.status_code}: {resp.text[:200]}", retryable=False)
        return resp.json()

    @staticmethod
    def _image_bytes(image: str) -> bytes:
        """We always upload the actual bytes — letting FB fetch a URL itself
        is a whole class of 'Missing or invalid image file' errors."""
        if image.startswith("http"):
            try:
                resp = httpx.get(image, timeout=30, follow_redirects=True,
                                 headers={"User-Agent": "TV3-Social-Autopilot/1.0"})
            except httpx.HTTPError as e:
                raise PublishError(f"attēlu neizdevās lejupielādēt: {e}",
                                   retryable=True) from e
            if resp.status_code != 200 or not resp.content:
                raise PublishError(
                    f"attēlu neizdevās lejupielādēt ({resp.status_code}): {image[:80]}",
                    retryable=True)
            return resp.content
        path = Path(image)
        if not path.exists() or path.stat().st_size == 0:
            raise PublishError(
                "attēla fails vairs neeksistē (renderēts pirms restarta) — "
                "sistēma to pārģenerēs nākamajā mēģinājumā", retryable=True)
        return path.read_bytes()

    def _upload_photo(self, image: str, extra: dict) -> dict:
        payload = self._image_bytes(image)
        return self._post(f"{self.page_id}/photos", extra,
                          files={"source": ("image.png", payload, "image/png")})

    def comment(self, post_id: str, message: str) -> str:
        """First-comment link strategy: photo posts keep the caption clean,
        the article link lands as the first comment."""
        return self._post(f"{post_id}/comments", {"message": message}).get("id", "")

    def _upload_video(self, endpoint: str, video: str) -> str:
        """Shared start -> binary upload phase of the resumable video flow
        (video_reels and video_stories use the same protocol)."""
        init = self._post(f"{self.page_id}/{endpoint}", {"upload_phase": "start"})
        video_id = init["video_id"]
        upload_url = (init.get("upload_url")
                      or f"https://rupload.facebook.com/video-upload/v21.0/{video_id}")
        payload = self._image_bytes(video)  # any local/remote file -> bytes
        resp = httpx.post(upload_url, content=payload, timeout=300, headers={
            "Authorization": f"OAuth {self.token}",
            "offset": "0", "file_size": str(len(payload)),
            "Content-Type": "application/octet-stream"})
        if resp.status_code >= 400:
            raise PublishError(f"FB video upload {resp.status_code}: {resp.text[:200]}",
                               retryable=True)
        return video_id

    def _publish_reel(self, video: str, description: str) -> str:
        """FB Reels three-step flow: start -> binary upload -> finish."""
        video_id = self._upload_video("video_reels", video)
        self._post(f"{self.page_id}/video_reels", {
            "upload_phase": "finish", "video_id": video_id,
            "video_state": "PUBLISHED", "description": description})
        return video_id

    def _publish_video_story(self, video: str) -> str:
        """FB video story via /video_stories (no caption/description field)."""
        video_id = self._upload_video("video_stories", video)
        out = self._post(f"{self.page_id}/video_stories",
                         {"upload_phase": "finish", "video_id": video_id})
        return out.get("post_id") or video_id

    def _publish_link_carousel(self, text: str, link: str, images: list[str],
                               card_links: list[str] | None = None,
                               card_titles: list[str] | None = None) -> str:
        """Organic swipeable carousel: /feed with child_attachments — each
        card is an image + the article link, the TVNET-style format. Needs
        public image URLs (PUBLIC_BASE_URL). Returns '' when the carousel
        can't be built or FB rejects it, so the caller can fall back to the
        album collage instead of losing the post."""
        # max 5 cards; keep the cover, trim middle points, never drop the
        # closing CTA card (it is the last image the renderer produced)
        idxs = (list(range(len(images))) if len(images) <= 5
                else [0, 1, 2, 3, len(images) - 1])
        cards = []
        for i in idxs:
            url = public_image_url(images[i])
            if not url:
                return ""
            # digest karuselī katra kartīte ved uz SAVU rakstu
            card_link = (card_links[i] if card_links and i < len(card_links)
                         and card_links[i] else link)
            card = {"link": card_link, "picture": url}
            # name aizpilda FB kartītes teksta joslu zem attēla — bez tā
            # kartīte plūsmā izskatās kā attēls ar tukšu apakšu
            title = (card_titles[i] if card_titles and i < len(card_titles)
                     and card_titles[i] else "")
            if title:
                card["name"] = title[:80]
            cards.append(card)
        if len(cards) < 2:
            return ""
        data = {
            "message": text, "link": link,
            "child_attachments": json.dumps(cards),
            # keep OUR card order and OUR closing CTA card — no FB reshuffle,
            # no auto-generated end card on top of ours
            "multi_share_optimized": "false",
            "multi_share_end_card": "false",
        }
        try:
            return self._post(f"{self.page_id}/feed", data)["id"]
        except PublishError as e:
            if e.retryable:
                raise
            log.warning("FB link carousel rejected, falling back to album: %s", e)
            return ""

    def publish(self, *, text: str, link: str, images: list[str], fmt: str,
                card_links: list[str] | None = None,
                card_titles: list[str] | None = None,
                alt_text: str = "") -> str:
        # alt_text_custom ir vienīgais lauks, ko FB pieņem pie /photos;
        # video un saites to neatbalsta, tāpēc tur to nesūtām
        alt = {"alt_text_custom": alt_text} if alt_text else {}
        if fmt == "reel" and images:
            return self._publish_reel(images[0], text)
        if fmt == "story" and images:
            if is_video(images[0]):
                return self._publish_video_story(images[0])
            up = self._upload_photo(images[0], {"published": "false", **alt})
            out = self._post(f"{self.page_id}/photo_stories", {"photo_id": up["id"]})
            return out.get("post_id") or out.get("id", "")
        if fmt == "photo" and images:
            out = self._upload_photo(images[0], {"caption": text, **alt})
            return out.get("post_id") or out.get("id", "")
        if fmt == "card_carousel" and link and len(images) > 1:
            post_id = self._publish_link_carousel(text, link, images,
                                                  card_links, card_titles)
            if post_id:
                return post_id
            # no public URLs / FB rejected the carousel -> album collage below
        if fmt in ("photo_album", "card_carousel") and len(images) > 1:
            media_ids = []
            for img in images[:10]:
                out = self._upload_photo(img, {"published": "false", **alt})
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
