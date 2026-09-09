"""X (Twitter) API v2 with native OAuth 1.0a signing (no extra deps).

Paid tier required. Supports text/link tweets, photo tweets (media is
uploaded via the v1.1 media/upload endpoint, then attached to the v2
tweet), multi-image tweets (photo_album / card_carousel — up to four
images) and video tweets (reel — chunked INIT/APPEND/FINALIZE upload with
async processing). Since X link cards no longer show the headline, photo
posts with the branded title image + link in text are the best-practice
format for visual stories. Degrades gracefully on 429 (queue retries with
backoff). `comment()` posts a reply tweet, where the link goes when
rules.yaml x_link_in_reply is on.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets as pysecrets
import time
import logging
from pathlib import Path
from urllib.parse import quote

import httpx

from adapters.base import Adapter, PublishError, is_video

log = logging.getLogger(__name__)

TWEETS_URL = "https://api.twitter.com/2/tweets"
UPLOAD_URL = "https://upload.twitter.com/1.1/media/upload.json"
MEDIA_META_URL = "https://upload.twitter.com/1.1/media/metadata/create.json"
# OAuth 2.0 ceļš iet uz v2: v1.1 upload.twitter.com Bearer marķieri nepieņem.
V2_UPLOAD = "https://api.x.com/2/media/upload"
V2_META = "https://api.x.com/2/media/metadata"
# X pieļauj līdz četriem attēliem vienā tvītā
MAX_IMAGES = 4
# chunked upload: X pieņem gabalus līdz 5 MB; 4 MB atstāj rezervi
CHUNK_BYTES = 4 * 1024 * 1024
VIDEO_PROCESS_TIMEOUT = 300


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

        # Divi ceļi. Jaunais: viena poga administrācijā, OAuth 2.0 marķieris,
        # kas pats atjaunojas. Vecais: četras OAuth 1.0a atslēgas, ielīmētas ar
        # roku. Jaunais ir priekšā, bet veco nelaužam — kam tas strādā, tam
        # jāturpina strādāt bez pieskaršanās.
        self.bearer = credentials.x_access_token()
        self.api_key = credentials.get("x_api_key")
        self.api_secret = credentials.get("x_api_secret")
        self.access_token = credentials.get("x_access_token")
        self.access_secret = credentials.get("x_access_secret")

    def configured(self) -> bool:
        return bool(self.bearer) or all(
            [self.api_key, self.api_secret, self.access_token, self.access_secret])

    @property
    def oauth2(self) -> bool:
        return bool(self.bearer)

    def _auth(self, method: str, url: str, extra_params: dict | None = None) -> dict:
        if self.bearer:
            return {"Authorization": f"Bearer {self.bearer}"}
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

    @staticmethod
    def _read(media: str) -> bytes:
        if media.startswith("http"):
            r = httpx.get(media, timeout=60, follow_redirects=True)
            r.raise_for_status()
            return r.content
        return Path(media).read_bytes()

    def _upload_media(self, image: str) -> str:
        payload = self._read(image)
        if self.oauth2:
            # v2 atbild ar {"data": {"id": ...}}, v1.1 — ar media_id_string
            resp = httpx.post(V2_UPLOAD, headers=self._auth("POST", V2_UPLOAD),
                              files={"media": payload},
                              data={"media_category": "tweet_image"}, timeout=60)
            self._check(resp, "media upload")
            body = resp.json()
            return str((body.get("data") or body).get("id")
                       or body.get("media_id_string"))
        resp = httpx.post(UPLOAD_URL, headers=self._auth("POST", UPLOAD_URL),
                          files={"media": payload}, timeout=60)
        self._check(resp, "media upload")
        return resp.json()["media_id_string"]

    def _upload_form(self, params: dict, what: str) -> dict:
        """v1.1 media/upload ar form parametriem (INIT/FINALIZE). Formas lauki
        ietilpst OAuth parakstā — atšķirībā no multipart APPEND."""
        resp = httpx.post(UPLOAD_URL, data=params,
                          headers=self._auth("POST", UPLOAD_URL, params),
                          timeout=60)
        self._check(resp, what)
        return resp.json() if resp.content else {}

    def _upload_video_v2(self, payload: bytes) -> str:
        """Tas pats trīssoļu ceļš uz v2 galapunktiem (OAuth 2.0).

        v1.1 upload.twitter.com Bearer marķieri nepieņem, tāpēc OAuth 2.0
        pieslēgumam ir savi galapunkti: /initialize, /{id}/append, /{id}/finalize.
        """
        init = httpx.post(f"{V2_UPLOAD}/initialize",
                          headers=self._auth("POST", V2_UPLOAD),
                          json={"total_bytes": len(payload),
                                "media_type": "video/mp4",
                                "media_category": "tweet_video"}, timeout=60)
        self._check(init, "video INIT")
        media_id = str((init.json().get("data") or {}).get("id"))
        for index, start in enumerate(range(0, len(payload), CHUNK_BYTES)):
            resp = httpx.post(f"{V2_UPLOAD}/{media_id}/append",
                              headers=self._auth("POST", V2_UPLOAD),
                              data={"segment_index": str(index)},
                              files={"media": payload[start:start + CHUNK_BYTES]},
                              timeout=120)
            self._check(resp, f"video APPEND {index}")
        fin = httpx.post(f"{V2_UPLOAD}/{media_id}/finalize",
                         headers=self._auth("POST", V2_UPLOAD), timeout=60)
        self._check(fin, "video FINALIZE")
        data = fin.json().get("data") or {}
        self._wait_processed(media_id, data.get("processing_info") or {})
        return media_id

    def _upload_video(self, video: str) -> str:
        """Chunked upload: INIT → APPEND pa gabaliem → FINALIZE → STATUS.

        Video X apstrādā asinhroni; tvīts ar vēl neapstrādātu media_id
        neizdodas, tāpēc gaidām, kamēr STATUS saka succeeded.
        """
        payload = self._read(video)
        if self.oauth2:
            return self._upload_video_v2(payload)
        init = self._upload_form({"command": "INIT", "total_bytes": str(len(payload)),
                                  "media_type": "video/mp4",
                                  "media_category": "tweet_video"}, "video INIT")
        media_id = str(init["media_id_string"])
        for index, start in enumerate(range(0, len(payload), CHUNK_BYTES)):
            resp = httpx.post(UPLOAD_URL, headers=self._auth("POST", UPLOAD_URL),
                              data={"command": "APPEND", "media_id": media_id,
                                    "segment_index": str(index)},
                              files={"media": payload[start:start + CHUNK_BYTES]},
                              timeout=120)
            self._check(resp, f"video APPEND {index}")
        fin = self._upload_form({"command": "FINALIZE", "media_id": media_id},
                                "video FINALIZE")
        self._wait_processed(media_id, fin.get("processing_info") or {})
        return media_id

    def _wait_processed(self, media_id: str, info: dict,
                        timeout: int = VIDEO_PROCESS_TIMEOUT) -> None:
        deadline = time.monotonic() + timeout
        while True:
            state = info.get("state")
            if state in (None, "succeeded"):
                return
            if state == "failed":
                err = (info.get("error") or {}).get("message", "")
                raise PublishError(f"X video apstrāde neizdevās: {err}"[:200],
                                   retryable=False)
            if time.monotonic() > deadline:
                raise PublishError("X video apstrāde pārsniedza laika limitu",
                                   retryable=True)
            time.sleep(min(int(info.get("check_after_secs") or 5), 30))
            if self.oauth2:
                resp = httpx.get(V2_UPLOAD, params={"media_id": media_id},
                                 headers=self._auth("GET", V2_UPLOAD), timeout=30)
                self._check(resp, "video STATUS")
                info = (resp.json().get("data") or {}).get("processing_info") or {}
                continue
            params = {"command": "STATUS", "media_id": media_id}
            resp = httpx.get(UPLOAD_URL, params=params,
                             headers=self._auth("GET", UPLOAD_URL, params),
                             timeout=30)
            self._check(resp, "video STATUS")
            info = resp.json().get("processing_info") or {}

    def _set_alt_text(self, media_id: str, alt_text: str) -> None:
        """Attēla apraksts ekrānlasītājiem (v1.1 media/metadata/create).

        Neizdošanās nedrīkst apturēt ierakstu: bez apraksta tvīts ir sliktāks,
        bez tvīta — nav nekāda.
        """
        try:
            if self.oauth2:
                httpx.post(V2_META, headers=self._auth("POST", V2_META),
                           json={"id": media_id,
                                 "metadata": {"alt_text": {"text": alt_text[:1000]}}},
                           timeout=30)
                return
            httpx.post(MEDIA_META_URL,
                       headers=self._auth("POST", MEDIA_META_URL),
                       json={"media_id": media_id,
                             "alt_text": {"text": alt_text[:1000]}},
                       timeout=30)
        except (httpx.HTTPError, OSError) as e:
            log.warning("X alt text failed for %s: %s", media_id, e)

    def publish(self, *, text: str, link: str, images: list[str], fmt: str,
                card_links: list[str] | None = None,
                card_titles: list[str] | None = None,
                alt_text: str = "") -> str:
        del card_links, card_titles  # X kartīšu saites/virsrakstus nepazīst
        payload: dict = {"text": text}
        media_ids: list[str] = []
        try:
            if fmt in ("reel", "video", "story") and images and is_video(images[0]):
                media_ids = [self._upload_video(images[0])]
            elif fmt in ("photo_album", "card_carousel") and images:
                # vairāki attēli vienā tvītā; kartīšu galerijai ņemam vāku un
                # pirmās nodaļas — beigas lai lasa rakstā
                for image in images[:MAX_IMAGES]:
                    media_id = self._upload_media(image)
                    if alt_text:
                        self._set_alt_text(media_id, alt_text)
                    media_ids.append(media_id)
            elif fmt in ("photo", "story") and images:
                media_ids = [self._upload_media(images[0])]
                if alt_text:
                    self._set_alt_text(media_ids[0], alt_text)
        except (PublishError, httpx.HTTPError, OSError) as e:
            # media failed -> still worth posting the text+link tweet
            log.warning("X media upload failed (%s), posting text only: %s", fmt, e)
            media_ids = []
        if media_ids:
            payload["media"] = {"media_ids": media_ids}
        resp = httpx.post(TWEETS_URL, json=payload,
                          headers=self._auth("POST", TWEETS_URL), timeout=30)
        self._check(resp, "tweet")
        return resp.json()["data"]["id"]

    def comment(self, post_id: str, message: str) -> str:
        """Atbilde zem paša tvīta (pavediens) — tur nonāk saite, kad
        rules.yaml x_link_in_reply liek to turēt ārpus tvīta teksta."""
        resp = httpx.post(TWEETS_URL,
                          json={"text": message,
                                "reply": {"in_reply_to_tweet_id": str(post_id)}},
                          headers=self._auth("POST", TWEETS_URL), timeout=30)
        self._check(resp, "reply")
        return resp.json().get("data", {}).get("id", "")
