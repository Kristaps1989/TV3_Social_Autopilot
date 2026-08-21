from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app import config

connect_args = {}
engine_kwargs = {}
if config.DATABASE_URL.startswith("sqlite"):
    # allow the scheduler thread and web workers to share the connection pool
    connect_args["check_same_thread"] = False
    db_path = config.DATABASE_URL.replace("sqlite:///", "")
    if db_path and db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    else:
        # a single shared connection, or every thread gets its own empty DB
        from sqlalchemy.pool import StaticPool

        engine_kwargs["poolclass"] = StaticPool

engine = create_engine(config.DATABASE_URL, connect_args=connect_args,
                       future=True, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def get_session() -> Session:
    return SessionLocal()


def init_db() -> None:
    from app import models  # noqa: F401

    models.Base.metadata.create_all(engine)
