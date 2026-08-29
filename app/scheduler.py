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


def tokens_job():
    """Refresh the Threads token before it expires; alert on tokens
    that expire within 7 days."""
    from app import credentials
    from app.pipeline import alert

    session = get_session()
    try:
        for warning in credentials.maintain_tokens(session):
            alert(warning)
    except Exception:  # noqa: BLE001
        log.exception("token maintenance failed")
    finally:
        session.close()


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
    scheduler.add_job(tokens_job, "interval", hours=24,
                      id="tokens", max_instances=1, coalesce=True)
    from app import ads

    scheduler.add_job(lambda: _job(ads.tick), "interval", hours=1,
                      id="ads", max_instances=1, coalesce=True)
    from app import weekend

    scheduler.add_job(lambda: _job(weekend.run), "interval", hours=1,
                      id="weekend", max_instances=1, coalesce=True)
    from app.pipeline import weekly_report

    scheduler.add_job(lambda: _job(weekly_report), "cron",
                      day_of_week="mon", hour=5, minute=0,  # 07:00/08:00 Riga
                      id="weekly_report", max_instances=1, coalesce=True)
    from app.overview import weekly_ai_report

    scheduler.add_job(lambda: _job(weekly_ai_report), "cron",
                      day_of_week="mon", hour=5, minute=30,
                      id="weekly_ai_report", max_instances=1, coalesce=True)
    scheduler.start()
    return scheduler
