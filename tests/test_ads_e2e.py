"""End-to-end ads cycle with a mocked Graph API: settings -> plan -> launch
(auto mode) -> insights -> GA4 paid sessions -> reallocation -> auto-pause,
plus the approve flow through the UI."""
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import ads, credentials, ga4
from app.main import app
from app.models import AdEntry, Article, CreativeAsset, Post, utcnow


@pytest.fixture()
def client(session):
    with TestClient(app) as c:
        yield c


class FakeGraph:
    """Minimal Meta Graph/Marketing API: records writes, serves insights."""

    def __init__(self):
        self.created = {"campaigns": [], "adsets": [], "ads": [], "creatives": [],
                        "dark_posts": [], "images": []}
        self.status: dict[str, str] = {}
        self.budgets: dict[str, int] = {}
        self.insights_rows: list[dict] = []
        self.seq = 0

    def _id(self, prefix):
        self.seq += 1
        return f"{prefix}{self.seq}"

    def post(self, url, data=None, files=None, content=None, headers=None, timeout=None):
        data = dict(data or {})
        if url.endswith("/campaigns"):
            cid = self._id("camp")
            self.created["campaigns"].append((cid, data))
            return _resp({"id": cid})
        if url.endswith("/adsets"):
            aid = self._id("as")
            self.created["adsets"].append((aid, data))
            self.budgets[aid] = int(data["daily_budget"])
            return _resp({"id": aid})
        if url.endswith("/adcreatives"):
            crid = self._id("cr")
            self.created["creatives"].append((crid, data))
            return _resp({"id": crid})
        if url.endswith("/ads"):
            adid = self._id("ad")
            self.created["ads"].append((adid, data))
            return _resp({"id": adid})
        if url.endswith("/adimages"):
            self.created["images"].append(files)
            return _resp({"images": {"f": {"hash": "imghash1"}}})
        if url.endswith("/feed"):  # dark post
            pid = self._id("520_dark")
            self.created["dark_posts"].append((pid, data))
            return _resp({"id": pid})
        # status/budget updates: /<object_id>
        obj = url.rsplit("/", 1)[-1]
        if "status" in data:
            self.status[obj] = data["status"]
        if "daily_budget" in data:
            self.budgets[obj] = int(data["daily_budget"])
        return _resp({"success": True})

    def get(self, url, params=None, timeout=None, headers=None,
            follow_redirects=None):
        if url.endswith("/insights"):
            return _resp({"data": self.insights_rows})
        if "/me/permissions" in url:
            return _resp({"data": [{"permission": p, "status": "granted"}
                                   for p in ("ads_management", "ads_read")]})
        if "act_" in url:
            return _resp({"name": "TV3", "account_status": 1, "currency": "EUR",
                          "funding_source_details": {"id": "f1"}})
        return _resp({"data": []})


def _resp(payload):
    class R:
        status_code = 200
        text = ""
        content = b"img-bytes"   # adapters lejupielādē attēlus kā baitus

        def json(self):
            return payload

    return R()


@pytest.fixture()
def graph(monkeypatch):
    g = FakeGraph()
    import adapters.facebook as fbmod
    import adapters.meta_ads as mamod

    for mod in (fbmod, mamod):
        monkeypatch.setattr(mod.httpx, "post", g.post)
        monkeypatch.setattr(mod.httpx, "get", g.get)
    monkeypatch.setattr(httpx, "post", g.post)
    monkeypatch.setattr(httpx, "get", g.get)
    creds = {"fb_ad_account_id": "999", "fb_user_token": "utok",
             "fb_page_id": "520", "fb_page_token": "ptok",
             "anthropic_api_key": ""}
    real_get = credentials.get
    # tikai reklāmu atslēgas viltojam; auth un pārējais iet pa īsto ceļu
    monkeypatch.setattr(credentials, "get",
                        lambda key, session=None: (creds[key] if key in creds
                                                   else real_get(key, session)))
    return g


def _published(session, guid, title, score=0.9, platform_post_id="520_111"):
    a = Article(guid=guid, url=f"https://tv3.lv/{guid}",
                canonical_url=f"https://tv3.lv/{guid}", title=title,
                section="sport", ai_score=score, images=["https://cdn/i.jpg"],
                raw_json={"_boostable": True, "_boost_reason": "droši"})
    session.add(a)
    session.flush()
    p = Post(article_id=a.id, channel="fb_tv3lv", format="link", copy=title,
             link_url=a.canonical_url, state="published",
             platform_post_id=platform_post_id,
             published_at=utcnow() - timedelta(hours=3))
    session.add(p)
    session.commit()
    return a, p


def test_full_auto_cycle(session, graph, monkeypatch):
    monkeypatch.setattr(ga4, "paid_sessions", lambda s, days=7: {})
    ads.save_settings(session, "auto", 20.0, 0)
    a1, p1 = _published(session, "e2e-1", "Uzvara basketbolā", 0.9, "520_111")
    a2, p2 = _published(session, "e2e-2", "Hokeja fināls", 0.8, "520_222")

    ads.tick(session)

    entries = session.execute(select(AdEntry).order_by(AdEntry.id)).scalars().all()
    assert [e.status for e in entries] == ["active", "active"]
    # kampaņa izveidota vienreiz un aktivizēta
    assert len(graph.created["campaigns"]) == 1
    cid, cdata = graph.created["campaigns"][0]
    assert cdata["objective"] == "OUTCOME_TRAFFIC"
    assert graph.status[cid] == "ACTIVE"
    # 2 ad seti pa 10 € (1000 centi), abi aktīvi
    assert sorted(graph.budgets[aid] for aid, _ in graph.created["adsets"]) == [1000, 1000]
    for aid, adata in graph.created["adsets"]:
        assert graph.status[aid] == "ACTIVE"
        assert adata["optimization_goal"] == "LINK_CLICKS"  # bez pixel
    # katram ierakstam boost + variantu (asset_feed_spec) reklāma
    assert len(graph.created["ads"]) == 4
    boost_creatives = [d for _, d in graph.created["creatives"]
                       if "object_story_id" in d]
    flex_creatives = [d for _, d in graph.created["creatives"]
                      if "asset_feed_spec" in d]
    assert len(boost_creatives) == 2 and len(flex_creatives) == 2
    import json as _json

    spec = _json.loads(flex_creatives[0]["asset_feed_spec"])
    assert spec["images"][0]["hash"] == "imghash1"
    link = spec["link_urls"][0]["website_url"]
    assert "utm_source=facebook_paid" in link
    assert f"utm_content=a{entries[0].id}" in link

    # --- metrikas: viena reklāma strādā, otra tērē bez rezultāta ----------
    e1, e2 = entries
    graph.insights_rows = [
        {"ad_id": e1.ad_id, "spend": "6.00", "impressions": "9000",
         "clicks": "60", "inline_link_clicks": "50"},
        {"ad_id": e2.ad_id, "spend": "6.00", "impressions": "8000",
         "clicks": "3", "inline_link_clicks": "2"},
    ]
    monkeypatch.setattr(ga4, "paid_sessions",
                        lambda s, days=7: {f"a{e1.id}": 40, f"a{e2.id}": 1})
    ads.collect_metrics(session, __import__("adapters.meta_ads", fromlist=["x"]).MetaAdsClient(session))
    session.refresh(e1); session.refresh(e2)
    assert e1.spent_cents == 600 and e1.clicks == 50 and e1.sessions == 40
    assert e2.sessions == 1

    # --- pārdale: uzvarētājam +20%, zaudētājs apstājas --------------------
    from adapters.meta_ads import MetaAdsClient

    ads.reallocate(session, MetaAdsClient(session))
    session.refresh(e1); session.refresh(e2)
    assert e2.status == "paused" and "auto-pauze" in e2.reason
    assert graph.status[e2.adset_id] == "PAUSED"
    assert e1.budget_cents == 1200          # 1000 * 1.2
    assert graph.budgets[e1.adset_id] == 1200
    # otrreiz tajā pašā dienā pārdale nenotiek (learning phase disciplīna)
    ads.reallocate(session, MetaAdsClient(session))
    session.refresh(e1)
    assert e1.budget_cents == 1200


def test_approve_flow_via_ui(session, graph, client, monkeypatch):
    monkeypatch.setattr(ga4, "paid_sessions", lambda s, days=7: {})
    client.post("/setup", data={"password": "slepens123", "password2": "slepens123"})
    ads.save_settings(session, "approve", 10.0, 0)
    _published(session, "e2e-3", "Futbola izloze", 0.9, "520_333")

    ads.tick(session)
    entry = session.execute(select(AdEntry)).scalars().one()
    assert entry.status == "awaiting_approval"
    assert graph.created["ads"] == []       # nekas vēl nav palaists

    r = client.get("/ads")
    assert "gaida apstiprinājumu" in r.text and "Palaist" in r.text

    r = client.post(f"/ads/{entry.id}/approve", follow_redirects=False)
    assert r.status_code == 303
    session.expire_all()
    entry = session.get(AdEntry, entry.id)
    assert entry.status == "active" and entry.adset_id
    assert len(graph.created["ads"]) == 2   # boost + varianti

    # manuāla pauze un atsākšana
    client.post(f"/ads/{entry.id}/pause")
    session.expire_all()
    assert session.get(AdEntry, entry.id).status == "paused"
    assert graph.status[entry.adset_id] == "PAUSED"
    client.post(f"/ads/{entry.id}/resume")
    session.expire_all()
    assert session.get(AdEntry, entry.id).status == "active"


def test_planner_creative_feeds_the_variants(session, graph, client, monkeypatch, tmp_path):
    client.post("/setup", data={"password": "slepens123", "password2": "slepens123"})
    a, p = _published(session, "e2e-4", "Recepšu tests")
    r = client.post("/ads/creative",
                    data={"article_url": a.canonical_url},
                    files={"image": ("mans.png", b"png-bytes", "image/png")},
                    follow_redirects=False)
    assert r.status_code == 303 and "saved" in r.headers["location"]
    asset = session.execute(select(CreativeAsset)).scalars().one()
    assert asset.article_id == a.id
    session.expire_all()
    imgs = ads.creative_images(session.get(Article, a.id), p)
    assert imgs[0] == asset.path            # plānotāja materiāls ir pirmais


def test_ad_copy_variants_fallback_without_key(session, monkeypatch):
    monkeypatch.setattr(credentials, "get", lambda key, session=None: "")
    a = Article(guid="v-x", url="u", canonical_url="u",
                title="Virsraksts par sportu", section="sport")
    assert ads.ad_copy_variants(a, session) == ["Virsraksts par sportu"]


def test_launch_failure_marks_entry_rejected(session, graph, monkeypatch):
    monkeypatch.setattr(ga4, "paid_sessions", lambda s, days=7: {})
    ads.save_settings(session, "auto", 10.0, 0)
    _published(session, "e2e-5", "Sporta ziņa", 0.9, "520_555")

    def boom(*a, **k):
        raise RuntimeError("api down")

    import app.ads as ads_mod

    monkeypatch.setattr(ads_mod, "launch_entry", boom)
    ads.tick(session)
    entry = session.execute(select(AdEntry)).scalars().one()
    assert entry.status == "rejected" and "palaišana neizdevās" in entry.reason


def test_x_ads_client_gated_and_signed(monkeypatch):
    from adapters.x_ads import XAdsClient

    monkeypatch.setattr(credentials, "get", lambda key, session=None: "")
    assert not XAdsClient().configured()

    values = {"x_ads_account_id": "abc1", "x_api_key": "k", "x_api_secret": "s",
              "x_access_token": "t", "x_access_secret": "ts"}
    monkeypatch.setattr(credentials, "get",
                        lambda key, session=None: values.get(key, ""))
    calls = {}

    def fake_post(url, data=None, timeout=None, headers=None):
        calls["url"], calls["data"], calls["headers"] = url, data, headers
        return _resp({"data": {"id": "c1"}})

    import adapters.x_ads as xa

    monkeypatch.setattr(xa.httpx, "post", fake_post)
    client = XAdsClient()
    assert client.configured()
    out = client.create_campaign("Tests", "fi1", 1000)
    assert out == "c1"
    assert "/accounts/abc1/campaigns" in calls["url"]
    assert calls["data"]["daily_budget_amount_local_micro"] == "10000000"
    assert calls["headers"]["Authorization"].startswith("OAuth ")


def test_new_candidates_share_only_the_remaining_budget(session, graph, monkeypatch):
    """Aktīvās reklāmas tur savu budžetu — jaunie kandidāti nedublē limitu."""
    monkeypatch.setattr(ga4, "paid_sessions", lambda s, days=7: {})
    ads.save_settings(session, "auto", 20.0, 0)
    _published(session, "e2e-6", "Pirmā sporta ziņa", 0.9, "520_611")
    ads.tick(session)
    first = session.execute(select(AdEntry)).scalars().one()
    assert first.status == "active" and first.budget_cents == 2000  # viss budžets

    _published(session, "e2e-7", "Otrā sporta ziņa", 0.8, "520_622")
    plan = ads.build_plan(session)
    # 20 € jau aktīvi -> jaunajam nekas nepaliek, tas stāv rindā ar iemeslu
    assert plan["planned"] == []
    assert plan["committed_eur"] == 20.0
    assert any("aktīvās reklāmas" in r["reason"] for r in plan["rejected"])
    # budžeta palielinājums -> atlikums aiziet jaunajam
    ads.save_settings(session, "auto", 30.0, 0)
    plan = ads.build_plan(session)
    assert len(plan["planned"]) == 1
    assert plan["planned"][0]["budget_eur"] == 10.0
