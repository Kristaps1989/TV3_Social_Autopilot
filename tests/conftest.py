import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("DISABLE_SCHEDULER", "true")
os.environ.setdefault("ANTHROPIC_API_KEY", "")

import pytest  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402


@pytest.fixture()
def session():
    init_db()
    s = SessionLocal()
    yield s
    s.rollback()
    # wipe tables between tests (shared in-memory engine)
    from app import models

    for table in reversed(models.Base.metadata.sorted_tables):
        s.execute(table.delete())
    s.commit()
    s.close()
