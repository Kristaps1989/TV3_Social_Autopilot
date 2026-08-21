from __future__ import annotations

from app import config
from adapters.base import Adapter, DryRunAdapter
from adapters.facebook import FacebookPageAdapter
from adapters.threads import ThreadsAdapter
from adapters.x import XAdapter

_REAL = {
    "facebook_page": FacebookPageAdapter,
    "x": XAdapter,
    "threads": ThreadsAdapter,
}


def get_adapter(platform: str) -> Adapter:
    if config.DRY_RUN:
        return DryRunAdapter(platform)
    cls = _REAL.get(platform)
    if cls is None:
        return DryRunAdapter(platform)
    adapter = cls()
    if not adapter.configured():
        # missing tokens: never crash the queue, degrade to dry run
        return DryRunAdapter(platform, note="missing credentials")
    return adapter
