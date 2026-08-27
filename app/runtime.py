"""Runtime switches stored in the DB (changeable from the admin UI without
a redeploy), with env defaults."""
from __future__ import annotations

import os
from pathlib import Path

from app import config
from app.db import get_session
from app.models import get_setting, set_setting


def is_dry_run(session=None) -> bool:
    """DRY RUN state: the admin-UI toggle (live_mode setting) wins;
    the DRY_RUN env var is only the initial default."""
    own = session is None
    if own:
        session = get_session()
    try:
        mode = get_setting(session, "live_mode")
        if mode == "on":
            return False
        if mode == "off":
            return True
        return config.DRY_RUN
    finally:
        if own:
            session.close()


def set_live(session, live: bool) -> None:
    set_setting(session, "live_mode", "on" if live else "off")


def data_dir_persistent() -> bool | None:
    """True if the data dir sits on a mounted volume, False if it is on the
    ephemeral container disk, None when not applicable (local dev)."""
    if not os.environ.get("RAILWAY_ENVIRONMENT") and not os.environ.get("RAILWAY_PROJECT_ID"):
        return None
    data_dir = Path(config.BASE_DIR) / "data"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        return os.path.ismount(data_dir)
    except OSError:
        return False
