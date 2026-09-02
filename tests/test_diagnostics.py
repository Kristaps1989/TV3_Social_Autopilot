"""Diagnostikas sadaļa (/logs), JSON eksports un žurnāla buferis."""
import logging
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import config, diagnostics, logbuffer
from app.main import app
from app.models import AdEntry, Article, Post, utcnow


@pytest.fixture()
def client(session):
    with TestClient(app) as c:
        c.post("/setup", data={"password": "slepens123", "password2": "slepens123"})
        yield c


def _feed(session, formats, channel="fb_tv3lv"):
    a = Article(guid="dg-1", url="https://tv3.lv/dg", canonical_url="https://tv3.lv/dg",
                title="Diagnostikas raksts", section="news",
                images=["https://cdn/i.jpg"], published_at=utcnow() - timedelta(hours=1))
    session.add(a)
    session.flush()
    base = utcnow() - timedelta(hours=3)
    for i, fmt in enumerate(formats):
        session.add(Post(article_id=a.id, channel=channel, format=fmt, copy="x",
                         state="published", created_at=base + timedelta(minutes=i),
                         published_at=base + timedelta(minutes=i),
                         extra={"format_notes": [f"{fmt} → piezīme"]} if i == 0 else {}))
    session.commit()
    return a


def test_report_shows_which_guard_holds_each_format(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    _feed(session, ["link", "photo", "photo", "photo"])
    data = diagnostics.report(session, channel="fb_tv3lv")
    ch = data["channels"][0]
    assert ch["run"] == {"format": "photo", "count": 3}
    status = {s["format"]: s for s in ch["status"]}
    assert "pēc kārtas" in status["photo"]["blocked"]
    assert status["link"]["starved"] is True          # grīda 40 %, daļa 25 %
    assert status["card_carousel"]["cap"] == 2
    assert data["editable_rules_used"] is False
    assert ch["history"][0]["notes"] == []            # jaunākais bez piezīmes
    assert any(h["notes"] for h in ch["history"])


def test_report_summarises_paid_results_per_format(session):
    a = _feed(session, ["link", "photo"])
    posts = session.query(Post).all()
    for p, sessions in zip(posts, (90, 20)):
        session.add(AdEntry(post_id=p.id, article_id=a.id, platform="facebook_page",
                            status="done", spent_cents=1000, sessions=sessions))
    session.commit()
    ads = {r["format"]: r for r in diagnostics.report(session)["ads"]}
    assert ads["link"]["per_eur"] == 9.0 and ads["photo"]["per_eur"] == 2.0
    assert ads["link"]["ads"] == 1 and ads["link"]["eur"] == 10.0


def test_simulation_explains_what_would_happen_now(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    _feed(session, ["photo", "photo"])
    data = diagnostics.report(session, channel="fb_tv3lv", simulate_article=True)
    rows = {r["ai_choice"]: r for r in data["simulation"]["channels"]["fb_tv3lv"]}
    assert rows["photo"]["chosen"] != "photo"          # divi foto pēc kārtas
    assert "pēc kārtas" in rows["photo"]["blocked"]["photo"]
    assert "gate" in rows["card_carousel"]


def test_log_buffer_keeps_recent_lines_and_hides_secrets():
    logbuffer.clear()
    logbuffer.install()
    # pytest saknes žurnālu tur uz WARNING; ražošanā to uz INFO noliek
    # `logging.basicConfig` app/main.py
    logging.getLogger().setLevel(logging.INFO)
    log = logging.getLogger("app.test_diag")
    log.info("format fb_tv3lv: link (grīda)")
    log.warning("token sk-ant-abc123456789012345678901234567890 nedrīkst parādīties")
    rows = logbuffer.records(limit=10)
    assert rows[0]["level"] == "WARNING" and "sk-ant-" not in rows[0]["message"]
    assert "«noslēpums»" in rows[0]["message"]
    assert rows[1]["message"].startswith("format fb_tv3lv")
    assert logbuffer.records(level="WARNING")[0]["level"] == "WARNING"
    assert logbuffer.records(contains="grīda")[0]["logger"] == "app.test_diag"
    assert logbuffer.records(contains="nav tāda") == []


def test_logs_page_and_export(session, client, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    _feed(session, ["photo", "photo", "photo"])
    logbuffer.clear()
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("app.test_diag").info("format fb_tv3lv: link (grīda)")

    page = client.get("/logs")
    assert page.status_code == 200
    assert "Diagnostika" in page.text and "pēc kārtas" in page.text
    assert "format fb_tv3lv: link" in page.text

    export = client.get("/logs/export.json")
    assert export.status_code == 200
    assert "attachment" in export.headers["content-disposition"]
    payload = export.json()
    assert payload["diagnostics"]["channels"][0]["run"]["count"] == 3
    assert any("format fb_tv3lv" in r["message"] for r in payload["log"])
    assert "simulation" in payload["diagnostics"]
