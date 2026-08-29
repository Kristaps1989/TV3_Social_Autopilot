from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import ads
from app.main import app
from app.models import AdEntry, Article, Post, utcnow


@pytest.fixture()
def client(session):
    with TestClient(app) as c:
        yield c


def _published(session, guid, title, section="sport", score=0.9, fmt="link",
               lead="", sensitivity=None, raw=None):
    a = Article(guid=guid, url=f"https://tv3.lv/{guid}",
                canonical_url=f"https://tv3.lv/{guid}", title=title, lead=lead,
                section=section, ai_score=score, sensitivity=sensitivity or [],
                raw_json=raw or {})
    session.add(a)
    session.flush()
    p = Post(article_id=a.id, channel="fb_tv3lv", format=fmt, copy=title,
             link_url=a.canonical_url, state="published",
             published_at=utcnow() - timedelta(hours=2))
    session.add(p)
    session.commit()
    return a, p


# --- boostability ----------------------------------------------------------

def test_politics_veto_beats_ai_yes(session):
    a, _ = _published(session, "ad-1", "Saeima lemj par nodokļiem",
                      section="news", raw={"_boostable": True})
    ok, reason = ads.boostable(a)
    assert not ok and "TTPA" in reason


def test_sensitivity_veto(session):
    a, _ = _published(session, "ad-2", "Smaga avārija uz šosejas",
                      sensitivity=["tragedy"])
    ok, reason = ads.boostable(a)
    assert not ok and "sensitīvs" in reason


def test_ai_verdict_respected(session):
    yes, _ = _published(session, "ad-3", "Kūku recepte rudenim",
                        section="entertainment",
                        raw={"_boostable": True, "_boost_reason": "droša tēma"})
    no, _ = _published(session, "ad-4", "Diskusija par nodokļu reformu",
                       section="news", raw={"_boostable": False,
                                            "_boost_reason": "sabiedrisks jautājums"})
    assert ads.boostable(yes) == (True, "droša tēma")
    ok, reason = ads.boostable(no)
    assert not ok and "sabiedrisks" in reason


def test_news_without_ai_verdict_stays_unboosted(session):
    a, _ = _published(session, "ad-5", "Notikums pilsētā", section="news")
    ok, reason = ads.boostable(a)
    assert not ok and "nav AI" in reason
    b, _ = _published(session, "ad-6", "Uzvara basketbolā", section="sport")
    assert ads.boostable(b)[0] is True


# --- plan ------------------------------------------------------------------

def test_plan_allocates_budget_and_keeps_reasons(session):
    ads.save_settings(session, "dry", 20.0, 20)
    _published(session, "ad-7", "Uzvara basketbolā", score=0.9)
    _published(session, "ad-8", "Kūku recepte", section="entertainment", score=0.8)
    _published(session, "ad-9", "Saeima lemj", section="news", score=0.95)
    plan = ads.build_plan(session)
    # 20 € - 20% zīmolam = 16 € konversijām -> 3 nesanāk pa 5 €, sanāk 3? 16//5=3
    titles = [r["article"].title for r in plan["planned"]]
    assert "Saeima lemj" not in titles
    assert set(titles) == {"Uzvara basketbolā", "Kūku recepte"}
    assert all(r["budget_eur"] == 8.0 for r in plan["planned"])
    assert plan["brand_eur"] == 4.0
    rejected_titles = [r["article"].title for r in plan["rejected"]]
    assert "Saeima lemj" in rejected_titles


def test_plan_too_small_budget_rejects_all(session):
    ads.save_settings(session, "dry", 4.0, 0)
    _published(session, "ad-10", "Uzvara hokejā")
    plan = ads.build_plan(session)
    assert plan["planned"] == []
    assert any("budžets" in r["reason"] for r in plan["rejected"])


def test_sync_entries_upserts_and_demotes(session):
    ads.save_settings(session, "dry", 10.0, 0)
    a, p = _published(session, "ad-11", "Uzvara futbolā")
    assert ads.sync_entries(session) == 1
    entry = session.execute(select(AdEntry)).scalars().one()
    assert entry.status == "planned" and entry.budget_cents == 1000

    # budžets uz nulli -> ieraksts izkrīt no plāna
    ads.save_settings(session, "dry", 0.0, 0)
    ads.sync_entries(session)
    session.refresh(entry)
    assert entry.status == "rejected"


def test_tick_off_mode_is_inert(session):
    ads.save_settings(session, "off", 50.0, 20)
    _published(session, "ad-12", "Uzvara volejbolā")
    ads.tick(session)
    assert session.execute(select(AdEntry)).scalars().all() == []


# --- meta ads adapter ------------------------------------------------------

def _ads_client(monkeypatch, creds=None):
    from adapters import meta_ads

    values = {"fb_ad_account_id": "123", "fb_user_token": "utok",
              "fb_page_id": "520", **(creds or {})}
    monkeypatch.setattr(meta_ads.credentials, "get",
                        lambda key, session=None: values.get(key, ""))
    return meta_ads.MetaAdsClient()


def test_readiness_reports_missing_pieces(monkeypatch):
    client = _ads_client(monkeypatch, {"fb_ad_account_id": ""})
    ok, issues = client.readiness()
    assert not ok and any("konts" in i for i in issues)


def test_readiness_checks_account_and_perms(monkeypatch):
    from adapters import meta_ads

    client = _ads_client(monkeypatch)
    monkeypatch.setattr(meta_ads.credentials, "fb_token_permissions",
                        lambda token: ["ads_read"])  # trūkst ads_management

    class R:
        status_code = 200
        text = ""

        def json(self):
            return {"name": "TV3", "account_status": 3, "currency": "EUR",
                    "funding_source_details": None}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: R())
    ok, issues = client.readiness()
    assert not ok
    joined = " ".join(issues)
    assert "ads_management" in joined
    assert "nenomaksāts" in joined and "maksājumu metodes" in joined


def test_create_adset_sends_budget_cents_and_lv_targeting(monkeypatch):
    import json as _json

    client = _ads_client(monkeypatch)
    calls = []

    class R:
        status_code = 200
        text = ""

        def json(self):
            return {"id": "as1"}

    def fake_post(url, data=None, timeout=None):
        calls.append((url, dict(data)))
        return R()

    monkeypatch.setattr(httpx, "post", fake_post)
    out = client.create_adset("c1", "test", 800)
    assert out == "as1"
    url, data = calls[0]
    assert "/act_123/adsets" in url
    assert data["daily_budget"] == "800"
    assert data["status"] == "PAUSED"
    targeting = _json.loads(data["targeting"])
    assert targeting["geo_locations"]["countries"] == ["LV"]


# --- UI --------------------------------------------------------------------

def _login(client):
    client.post("/setup", data={"password": "slepens123", "password2": "slepens123"})


def test_ads_page_shows_plan_and_saves_settings(client, session):
    _login(client)
    _published(session, "ad-20", "Uzvara basketbolā")
    r = client.post("/ads/settings", data={"mode": "dry", "daily_budget": "10",
                                           "brand_share": "20"},
                    follow_redirects=False)
    assert r.status_code == 303
    r = client.get("/ads")
    assert r.status_code == 200
    assert "Uzvara basketbolā" in r.text
    assert "dry-run" in r.text
    # 2. fāzes režīmi vēl nav ieslēdzami
    client.post("/ads/settings", data={"mode": "auto", "daily_budget": "10",
                                       "brand_share": "20"})
    assert ads.settings(session)["mode"] == "dry"


def test_connect_page_renders_meta_ads_card(client, session):
    _login(client)
    r = client.get("/connect")
    assert r.status_code == 200
    assert "Meta reklāmas" in r.text
    r = client.post("/connect/meta-ads", data={"ad_account_id": "act_555",
                                               "pixel_id": ""},
                    follow_redirects=False)
    assert r.status_code == 303 and "connected=meta_ads" in r.headers["location"]
    from app import credentials

    assert credentials.get("fb_ad_account_id", session) == "555"
