"""Threads via the official Threads API (two-step: create container, publish).

Formāti: teksts ar saites kartīti (TEXT + link_attachment), attēls (IMAGE),
video/lente (VIDEO — konteineru apstrādā asinhroni, tāpēc pirms publicēšanas
gaidām FINISHED) un karuselis (CAROUSEL no IMAGE/VIDEO bērniem). Saite
mediju ierakstos paliek tekstā — Threads to padara klikšķināmu pats;
`comment()` uzraksta atbildi zem ieraksta, ja noteikumi liek saiti tur.
"""
from __future__ import annotations

import logging
import time

import httpx

from adapters.base import Adapter, PublishError, is_video, public_image_url
from app import credentials

log = logging.getLogger(__name__)

API = "https://graph.threads.net/v1.0"
# Threads karuselī drīkst 2–20 bērnus; mūsu kartīšu galerija tik tālu netiek
CAROUSEL_MAX = 20
PROCESS_TIMEOUT = 240
POLL_SECONDS = 5


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

    def _container(self, data: dict, alt_text: str = "") -> str:
        """Konteinera id. Attēla aprakstu (alt_text) pieņem tikai IMAGE un
        VIDEO konteineri; ja API to noraida, mēģinām bez — bez apraksta
        ieraksts ir sliktāks, bez ieraksta nav nekāda."""
        if alt_text and data.get("media_type") in ("IMAGE", "VIDEO"):
            try:
                return self._post(f"{self.user_id}/threads",
                                  {**data, "alt_text": alt_text[:1000]})["id"]
            except PublishError as e:
                if e.retryable or "alt_text" not in str(e):
                    raise
                log.warning("Threads noraidīja alt_text, sūtām bez: %s", e)
        return self._post(f"{self.user_id}/threads", data)["id"]

    def _media_container(self, url: str, extra: dict, alt_text: str = "") -> str:
        if is_video(url):
            return self._container({"media_type": "VIDEO", "video_url": url, **extra},
                                   alt_text)
        return self._container({"media_type": "IMAGE", "image_url": url, **extra},
                               alt_text)

    def publish(self, *, text: str, link: str, images: list[str], fmt: str,
                card_links: list[str] | None = None,
                card_titles: list[str] | None = None,
                alt_text: str = "") -> str:
        del card_links, card_titles  # Threads karuselim nav kartīšu saišu
        urls = [u for u in (public_image_url(i) for i in images) if u]
        if fmt in ("photo_album", "card_carousel") and len(urls) >= 2:
            children = [self._media_container(u, {"is_carousel_item": "true"},
                                              alt_text)
                        for u in urls[:CAROUSEL_MAX]]
            container = self._container({"media_type": "CAROUSEL",
                                         "children": ",".join(children),
                                         "text": text})
            self._wait_processed(container)
        elif fmt in ("photo", "photo_album", "card_carousel", "reel", "story",
                     "video") and urls:
            container = self._media_container(urls[0], {"text": text}, alt_text)
            if is_video(urls[0]):
                self._wait_processed(container)
            else:
                time.sleep(2)  # Threads iesaka īsu pauzi pirms publicēšanas
        else:
            data = {"media_type": "TEXT", "text": text}
            if link:
                data["link_attachment"] = link
            container = self._container(data)
            time.sleep(2)
        return self._post(f"{self.user_id}/threads_publish",
                          {"creation_id": container})["id"]

    def _wait_processed(self, container_id: str, timeout: int = PROCESS_TIMEOUT) -> None:
        """Video un karuseļa konteinerus Threads apstrādā asinhroni; publicēt
        pirms FINISHED nozīmē kļūdu, tāpēc gaidām."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r = httpx.get(f"{API}/{container_id}", timeout=30,
                          params={"fields": "status,error_message",
                                  "access_token": self.token})
            body = r.json() if r.status_code == 200 else {}
            status = body.get("status")
            if status in ("FINISHED", "PUBLISHED"):
                return
            if status in ("ERROR", "EXPIRED"):
                raise PublishError(
                    f"Threads mediju apstrāde neizdevās ({status}): "
                    f"{body.get('error_message', '')}"[:200], retryable=False)
            time.sleep(POLL_SECONDS)
        raise PublishError("Threads mediju apstrāde pārsniedza laika limitu",
                           retryable=True)

    def comment(self, post_id: str, message: str) -> str:
        """Atbilde zem paša ieraksta — tur nonāk saite, ja noteikumi to liek
        ārpus teksta (threads_link_in_reply)."""
        container = self._container({"media_type": "TEXT", "text": message,
                                     "reply_to_id": post_id})
        time.sleep(2)
        return self._post(f"{self.user_id}/threads_publish",
                          {"creation_id": container}).get("id", "")

    def fetch_insights(self, platform_post_id: str) -> dict | None:
        """Threads mediju ieskats: views + likes (klikšķus dod GA4 utm)."""
        try:
            resp = httpx.get(f"{API}/{platform_post_id}/insights",
                             params={"metric": "views,likes",
                                     "access_token": self.token}, timeout=30)
            if resp.status_code != 200:
                return None
            values = {d["name"]: (d["values"][0]["value"] if d.get("values") else 0)
                      for d in resp.json().get("data", [])}
            return {"impressions": int(values.get("views", 0) or 0),
                    "clicks": 0,
                    "reactions": int(values.get("likes", 0) or 0)}
        except (httpx.HTTPError, ValueError, KeyError):
            return None
