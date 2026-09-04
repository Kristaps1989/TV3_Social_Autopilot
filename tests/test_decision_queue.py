"""Lemjamā rinda: atliktie raksti nedrīkst apēst cikla budžetu.

Play katalogs dod simtus rindu dienā, un dienas limits ir viens ieraksts —
pārējie atgriežas ar atlikšanas taimeri. Kamēr vaicājums ņēma vienkārši
divdesmit vecākos, visi divdesmit bija tieši šie gaidītāji, un neviena
svaiga ziņa vairs netika izlemta.
"""
from datetime import timedelta

from app import config, pipeline
from app.models import Article, Evaluation, utcnow


def _article(session, title, seen_ago_hours, feed="tv3"):
    url = f"https://tv3.lv/{title}"
    a = Article(guid=f"g-{title}", url=url, canonical_url=url, title=title,
                section="news", feed_name=feed, editor_status="can",
                first_seen_at=utcnow() - timedelta(hours=seen_ago_hours),
                published_at=utcnow() - timedelta(hours=seen_ago_hours),
                raw_json={})
    session.add(a)
    session.flush()
    return a


def test_backed_off_articles_do_not_crowd_out_fresh_news(session):
    now = utcnow()
    # 30 vecākas Play rindas, visas atliktas — vairāk nekā viena cikla budžets
    for i in range(30):
        old = _article(session, f"play-{i}", 20 + i, feed="play")
        pipeline.requeue_for_retry(old, now)
    fresh = _article(session, "svaiga-zina", 1)
    session.flush()

    batch = pipeline.undecided_batch(session, now, 20)
    assert [a.title for a in batch] == ["svaiga-zina"]
    assert fresh in batch


def test_batch_still_returns_articles_once_the_timer_has_passed(session):
    now = utcnow()
    a = _article(session, "gaidija", 5)
    pipeline.requeue_for_retry(a, now)
    session.flush()
    assert pipeline.undecided_batch(session, now, 20) == []
    # taimeris beidzies — raksts atgriežas rindā
    later = now + timedelta(hours=6)
    assert pipeline.undecided_batch(session, later, 20) == [a]


def test_queue_health_names_the_stall_and_the_guard(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    now = utcnow()
    for i in range(3):
        a = _article(session, f"play-{i}", 10 + i, feed="play")
        pipeline.requeue_for_retry(a, now)
        session.add(Evaluation(article_id=a.id, channel="fb_main", outcome="blocked",
                               reason="nav derīga laika: Play vakara logs"))
    session.flush()

    out = pipeline.queue_health(session, now)
    assert out["undecided"] == 3 and out["waiting_retry"] == 3 and out["ready"] == 0
    assert out["by_feed"] == {"play": 3}
    assert out["block_reasons"]["Play vakara logs"] == 3
