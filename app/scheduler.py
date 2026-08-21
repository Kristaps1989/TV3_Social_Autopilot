from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app import config
from app.db import get_session
from app.ingest import run_ingest
from app.pipeline import collect_metrics, paused, publish_due, run_decisions

log = logging.getLogger(__name__)


def _job(fn, *args):
    session = get_session()
    try:
        return fn(session, *args)
    except Exception:  # noqa: BLE001
        log.exception("job %s failed", fn.__name__)
    finally:
        session.close()


def ingest_job():
    summary = _job(run_ingest)
    if summary and (summary["created"] or summary["errors"]):
        log.info("ingest: %s", summary)


def decide_job():
    session = get_session()
    try:
        if paused(session):
            return
    finally:
        session.close()
    created = _job(run_decisions)
    if created:
        log.info("decide: %d posts scheduled", created)


def publish_job():
    n = _job(publish_due)
    if n:
        log.info("publish: %d posts published", n)


def metrics_job():
    _job(collect_metrics)


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(ingest_job, "interval", minutes=config.INGEST_INTERVAL_MINUTES,
                      id="ingest", max_instances=1, coalesce=True)
    scheduler.add_job(decide_job, "interval", minutes=2,
                      id="decide", max_instances=1, coalesce=True)
    scheduler.add_job(publish_job, "interval", minutes=1,
                      id="publish", max_instances=1, coalesce=True)
    scheduler.add_job(metrics_job, "interval", hours=1,
                      id="metrics", max_instances=1, coalesce=True)
    scheduler.start()
    return scheduler
