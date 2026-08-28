import pytest
from fastapi.testclient import TestClient

from app import ga4


@pytest.fixture()
def client(session, monkeypatch):
    from app.main import app

    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    with TestClient(app) as c:
        yield c


def test_delta_math():
    assert ga4._delta(110, 100)["change_pct"] == 10.0
    assert ga4._delta(50, 100)["change_pct"] == -50.0
    assert ga4._delta(5, 0)["change_pct"] is None   # no meaningful %
    assert ga4._delta(0, 0)["change_pct"] == 0.0


def test_section_for_path():
    m = {"zinas": "news", "sports": "sport"}
    assert ga4._section_for_path("/zinas/latvija/x/", m) == "news"
    assert ga4._section_for_path("/zinas/sports/futbols/", m) == "sport"
    assert ga4._section_for_path("/video/123/", m) == "cits"


def test_traffic_sources_with_fake_reports(monkeypatch):
    ga4._cache.clear()
    monkeypatch.setattr(ga4, "property_id", lambda: "123")
    calls = []

    def fake_report(prop, body):
        calls.append(body)
        first = body["dateRanges"][0]["startDate"] == "30daysAgo"
        rows = ([{"dimensionValues": [{"value": "Organic Social"}],
                  "metricValues": [{"value": "800"}]},
                 {"dimensionValues": [{"value": "Direct"}],
                  "metricValues": [{"value": "200"}]}] if first else
                [{"dimensionValues": [{"value": "Organic Social"}],
                  "metricValues": [{"value": "400"}]}])
        return {"rows": rows}

    monkeypatch.setattr(ga4, "_report", fake_report)
    rows = ga4.traffic_sources(days=30)
    assert rows[0]["channel"] == "Organic Social"
    assert rows[0]["sessions"] == 800
    assert rows[0]["pct"] == 80.0
    assert rows[0]["change_pct"] == 100.0
    assert rows[1]["change_pct"] is None  # Direct had no previous sessions


def test_dashboard_unconfigured(session, monkeypatch):
    monkeypatch.setattr(ga4, "configured", lambda: False)
    assert ga4.dashboard(session) == {"configured": False}


def test_portal_page_renders(client, session):
    from app import auth, credentials

    credentials.put(session, "admin_password_hash", auth.hash_password("slepens123"))
    client.post("/login", data={"password": "slepens123"})
    r = client.get("/portal")
    assert r.status_code == 200
    assert "GA4 nav pieslēgts" in r.text or "Portāls" in r.text
