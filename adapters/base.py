from __future__ import annotations

import logging
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)


class PublishError(Exception):
    """Raised on a failed publish. retryable=True → the queue retries with backoff."""

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class Adapter(ABC):
    platform: str = ""

    def configured(self) -> bool:
        return True

    @abstractmethod
    def publish(self, *, text: str, link: str, images: list[str], fmt: str) -> str:
        """Publish and return the platform post id."""

    def fetch_insights(self, platform_post_id: str) -> dict | None:
        """Return {impressions, clicks, reactions} or None if unavailable."""
        return None


class DryRunAdapter(Adapter):
    """Records what WOULD have been posted. Used in Phase 1 and whenever
    credentials are missing, so the pipeline never breaks."""

    def __init__(self, platform: str, note: str = ""):
        self.platform = platform
        self.note = note

    def publish(self, *, text: str, link: str, images: list[str], fmt: str) -> str:
        log.info("[DRY RUN %s] %s | %s | link=%s images=%d",
                 self.platform, fmt, text[:120], link, len(images))
        return f"dry-run{'-' + self.note if self.note else ''}"
