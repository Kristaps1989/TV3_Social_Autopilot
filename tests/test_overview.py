from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import ga4, overview
from app.main import app
from app.models import AdEntry, Article, Post, utcnow


@pytest.fixture()
def client(session):
    with TestClient(app) as c:
        yield c


def _content(session):
    a = Article(guid="ov-1", url="https://tv3.lv/ov", canonical_url="https://tv3.lv/ov",
                title="Raksts", section="news", decided_at=utcnow())
    session.add(a)
    session.flush()
    session.add(Post(article_id=a.id, channel="fb_tv3lv", format="link",
                     state="published", published_at=utcnow() - timedelta(hours=2)))
    session.add(Post(article_id=a.id, channel="fb_stories", format="story",
                     state="published", published_at=utcnow() - timedelta(hours=1)))
    session.add(AdEntry(post_id=1, article_id=a.id, status="active",
                        budget_cents=1000, spent_cents=600, clicks=50,
                        sessions=40, updated_at=utcnow()))
    session.commit()
    return a


def test_content_funnel_counts(session):
    _content(session)
    f = overview.content_funnel(session, 7)
    assert f["articles"] == 1 and f["published_articles"] == 1
    assert f["posts"] == 2 and f["by_format"] == {"link": 1, "story": 1}
    assert f["utilization"] == 100.0


def test_channel_economics_buckets_and_external_spend(session, monkeypatch):
    _content(session)
    overview.save_external_spend(session, 3000.0)
    monkeypatch.setattr(ga4, "channel_economics", lambda days=30: [
        {"channel": "Paid Search", "sessions": 30000, "engaged": 20000},
        {"channel": "Cross-network", "sessions": 10000, "engaged": 6000},
        {"channel": "Paid Social", "sessions": 4000, "engaged": 3000},
        {"channel": "Organic Social", "sessions": 50000, "engaged": 30000},
    ])
    eco = overview.channel_economics(session, days=30)
    # aģentūras Google: Paid Search + Cross-network, ~3000 € mēnesī
    assert eco["google"]["sessions"] == 40000
    assert 2900 < eco["google"]["spend"] < 3000
    assert 0.07 < eco["google"]["cps"] < 0.08
    # mūsu Meta reklāmas: tēriņš no AdEntry, sesijas no GA4 Paid Social
    assert eco["meta_paid"]["spend"] == 6.0
    assert eco["meta_paid"]["sessions"] == 4000
    assert eco["organic_social"]["sessions"] == 50000


def test_overview_page_renders(client, session, monkeypatch):
    client.post("/setup", data={"password": "slepens123", "password2": "slepens123"})
    _content(session)
    monkeypatch.setattr(ga4, "channel_economics", lambda days=30: [
        {"channel": "Paid Search", "sessions": 100, "engaged": 60}])
    r = client.get("/overview")
    assert r.status_code == 200
    assert "Kanālu ekonomika" in r.text and "AI mārketinga ieteikumi" in r.text
    # franšīžu režģis ar slēdžiem un snieguma tabula
    assert "Satura franšīzes" in r.text and "Franšīžu sniegums" in r.text
    assert "Kas notiek ar rakstiem" in r.text
    assert "Trešdienas jautājums" in r.text and "Dienas TOP 3" in r.text
    r = client.post("/overview/spend", data={"monthly_eur": "3000"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert overview.external_spend(session) == 3000.0


def test_ai_report_without_key_returns_hint(session, monkeypatch):
    from app import credentials

    monkeypatch.setattr(credentials, "get", lambda key, session=None: "")
    monkeypatch.setattr(ga4, "channel_economics", lambda days=30: [])
    out = overview.ai_report(session)
    assert "AI atslēga" in out


def test_franchise_stats_scores_against_the_editorial_baseline(session):
    from app.models import PostMetrics

    a = _content(session)

    def post(hook, sessions):
        p = Post(article_id=a.id, channel="fb_tv3lv", format="photo",
                 hook_type=hook, state="published",
                 published_at=utcnow() - timedelta(days=1))
        session.add(p)
        session.flush()
        session.add(PostMetrics(post_id=p.id, ga_sessions=sessions,
                                collected_at=utcnow()))

    for n in (100, 100, 100, 100):        # bāzes līnija: 100 sesijas/ierakstu
        post("", n)
    for n in (300, 300, 300):             # franšīze, kas pārspēj bāzi
        post("quiz", n)
    for n in (10, 10, 10):                # franšīze, kas atpaliek
        post("guide", n)
    post("number", 5)                     # tikai viens ieraksts -> par agru
    session.commit()

    stats = overview.franchise_stats(session, days=28)
    by_hook = {i["hook"]: i for i in stats["items"]}
    # _content() jau pievieno divus ierakstus bez hook_type -> 6 bāzes ieraksti
    assert stats["baseline_posts"] == 6
    assert by_hook["quiz"]["verdict"] == "turēt"
    assert by_hook["quiz"]["vs_benchmark"] > 4
    assert by_hook["guide"]["verdict"] == "vāji"
    assert by_hook["number"]["verdict"] == "par agru"
    # sakārtots pēc sesijām uz ierakstu
    assert stats["items"][0]["hook"] == "quiz"


def test_publication_funnel_separates_refusals_from_work_in_progress(session):
    from app.models import Evaluation

    def article(guid, **kw):
        a = Article(guid=guid, url=f"https://tv3.lv/{guid}",
                    canonical_url=f"https://tv3.lv/{guid}", title=guid,
                    section="news", **kw)
        session.add(a)
        session.flush()
        return a

    published = article("pf-1", decided_at=utcnow())
    session.add(Post(article_id=published.id, channel="fb_tv3lv", format="link",
                     state="published", published_at=utcnow()))
    queued = article("pf-2", decided_at=utcnow())
    session.add(Post(article_id=queued.id, channel="fb_tv3lv", format="link",
                     state="scheduled", scheduled_at=utcnow() + timedelta(hours=1)))
    fresh = article("pf-3")                       # vēl nav izvērtēts
    skipped = article("pf-4", decided_at=utcnow())
    session.add(Evaluation(article_id=skipped.id, channel="fb_tv3lv",
                           outcome="ai_skip", reason="tēma neatbilst kanālam"))
    blocked = article("pf-5", decided_at=utcnow())
    session.add(Evaluation(article_id=blocked.id, channel="fb_tv3lv",
                           outcome="blocked",
                           reason="too old: 52h > 24h limit for news"))
    dont = article("pf-6", editor_status="dont", decided_at=utcnow())
    session.commit()

    f = overview.publication_funnel(session, days=7)
    assert f["total"] == 6
    labels = {b["label"]: b["n"] for b in f["buckets"]}
    assert labels["Publicēts"] == 1
    assert labels["Vēl rindā (ieplānots)"] == 1
    assert labels["Vēl nav izvērtēts"] == 1
    assert labels["AI izlēma nepublicēt"] == 1
    assert labels["Noteikumi bloķēja"] == 1
    assert labels["Redaktors atzīmēja «nepublicēt»"] == 1
    # sīkie iemeslu teksti sagrupēti lasāmās grupās
    assert {"Raksts par vecu kanāla svaiguma limitam"} <= {
        r["label"] for r in f["reasons"]}
    assert all(b["pct"] > 0 for b in f["buckets"])
    assert fresh.id and queued.id and dont.id     # nav neizmantotu mainīgo
