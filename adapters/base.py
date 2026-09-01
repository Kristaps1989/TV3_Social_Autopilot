from __future__ import annotations

import logging
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)


class PublishError(Exception):
    """Raised on a failed publish. retryable=True → the queue retries with backoff."""

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


def is_video(media: str) -> bool:
    """Media entries are image paths/URLs except the pipeline's rendered
    clips (reels, video stories), which are always video files."""
    return media.split("?")[0].lower().endswith((".mp4", ".mov", ".m4v"))


def public_image_url(image: str) -> str:
    """Meta's URL-based upload APIs (Threads, Instagram) only accept public
    URLs — locally rendered images are served by the app's own /media
    endpoint, reachable at PUBLIC_BASE_URL."""
    if image.startswith("http"):
        return image
    import os
    from pathlib import Path

    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if not base:
        # Railway injects the app's public domain automatically
        domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
        base = f"https://{domain}" if domain else ""
    return f"{base}/media/{Path(image).name}" if base else ""


class Adapter(ABC):
    platform: str = ""

    def configured(self) -> bool:
        return True

    @abstractmethod
    def publish(self, *, text: str, link: str, images: list[str], fmt: str,
                card_links: list[str] | None = None,
                card_titles: list[str] | None = None,
                alt_text: str = "") -> str:
        """Publish and return the platform post id. card_links: per-card
        destinations for digest carousels (platforms without carousel link
        support ignore it). alt_text: attēla apraksts ekrānlasītājiem —
        platformas, kas to neatbalsta, to ignorē."""

    def fetch_insights(self, platform_post_id: str) -> dict | None:
        """Return {impressions, clicks, reactions} or None if unavailable."""
        return None

    def comment(self, post_id: str, message: str) -> str:
        """Post a comment under a published post (first-comment link).
        Adapters without comment support inherit this no-op."""
        return ""


class DryRunAdapter(Adapter):
    """Records what WOULD have been posted. Used in Phase 1 and whenever
    credentials are missing, so the pipeline never breaks."""

    def __init__(self, platform: str, note: str = ""):
        self.platform = platform
        self.note = note

    def publish(self, *, text: str, link: str, images: list[str], fmt: str,
                card_links: list[str] | None = None,
                card_titles: list[str] | None = None,
                alt_text: str = "") -> str:
        log.info("[DRY RUN %s] %s | %s | link=%s images=%d alt=%s",
                 self.platform, fmt, text[:120], link, len(images),
                 (alt_text or "—")[:60])
        return f"dry-run{'-' + self.note if self.note else ''}"

    def comment(self, post_id: str, message: str) -> str:
        log.info("[DRY RUN %s] first comment: %s", self.platform, message[:120])
        return "dry-run-comment"
