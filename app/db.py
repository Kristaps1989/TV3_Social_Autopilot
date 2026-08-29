from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app import config

connect_args = {}
engine_kwargs = {}
_sqlite_file = False
if config.DATABASE_URL.startswith("sqlite"):
    # allow the scheduler thread and web workers to share the connection pool
    connect_args["check_same_thread"] = False
    # wait for a concurrent writer instead of failing with "database is locked"
    connect_args["timeout"] = 30
    db_path = config.DATABASE_URL.replace("sqlite:///", "")
    if db_path and db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        _sqlite_file = True
    else:
        # a single shared connection, or every thread gets its own empty DB
        from sqlalchemy.pool import StaticPool

        engine_kwargs["poolclass"] = StaticPool

engine = create_engine(config.DATABASE_URL, connect_args=connect_args,
                       future=True, **engine_kwargs)

if _sqlite_file:
    # WAL lets the scheduler write while web requests read/write without
    # tripping over each other
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def get_session() -> Session:
    return SessionLocal()


def init_db() -> None:
    from app import models  # noqa: F401

    models.Base.metadata.create_all(engine)
    _migrate()


def _migrate() -> None:
    """Additive column migrations — create_all never alters existing tables."""
    from sqlalchemy import text

    for ddl in (
        "ALTER TABLE posts ADD COLUMN hook_type VARCHAR(16) DEFAULT ''",
        "ALTER TABLE posts ADD COLUMN short_hits INTEGER DEFAULT 0",
        "ALTER TABLE ads ADD COLUMN dark_ad_id VARCHAR(64) DEFAULT ''",
    ):
        try:
            with engine.begin() as conn:
                conn.execute(text(ddl))
        except Exception:  # noqa: BLE001 — column already exists
            pass
