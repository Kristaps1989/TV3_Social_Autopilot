from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import cards, regenerate, weekend
from app.main import app
from app.models import Article, Post, PostMetrics, utcnow


@pytest.fixture()
def client(session):
    with TestClient(app) as c:
        yield c


def _art(session, guid, title, section="news", sensitivity=None, sessions=100):
    a = Article(guid=guid, url=f"https://tv3.lv/{guid}",
                canonical_url=f"https://tv3.lv/{guid}", title=title,
                section=section, ai_score=0.8,
                images=[f"https://cdn/uploads/{guid}.jpg"],
                sensitivity=sensitivity or [],
                published_at=utcnow() - timedelta(days=1))
    session.add(a)
    session.flush()
    p = Post(article_id=a.id, channel="fb_tv3lv", format="link", copy=title,
             link_url=a.canonical_url, state="published",
             published_at=utcnow() - timedelta(hours=3))
    session.add(p)
    session.flush()
    session.add(PostMetrics(post_id=p.id, ga_sessions=sessions,
                            collected_at=utcnow()))
    session.commit()
    return a


# --- ētika: izklaidējošie formāti neaiztiek traģēdijas -------------------

def test_grim_stories_never_become_a_game():
    class A:
        sensitivity = []
        title = "Cik cilvēku dzīvību prasījuši plūdi Nepālā"

    assert weekend.playful_safe(A()) is False
    A.title = "Latvija uzvar Igauniju ar 3:1"
    assert weekend.playful_safe(A()) is True
    # AI jutīguma birka ir pirmā aizsardzība, vārdu saraksts — otrā
    A.sensitivity = ["crime"]
    assert weekend.playful_safe(A()) is False
    assert weekend.grim_words("Trīs gājuši bojā avārijā") != ""
    assert weekend.grim_words("Koncertzāle atvērta apmeklētājiem") == ""


def test_quiz_skips_tragedies_even_when_they_are_the_most_read(session,
                                                               monkeypatch):
    _art(session, "q-grim", "Plūdi Nepālā prasījuši desmitiem dzīvību",
         sessions=9000)
    for i in range(3):
        _art(session, f"q-ok-{i}", f"Sporta notikums numur {i}", sessions=100 - i)
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    seen = {}

    def fake_cards(title, section, tag, points, image, question, **kwargs):
        seen.update(points=points, image=image)
        return ["c0.png", "c1.png", "c2.png", "c3.png", "c4.png"]

    monkeypatch.setattr(cards, "render_cards", fake_cards)
    monkeypatch.setattr(weekend, "_ai_lines", lambda *a, **k: [
        "Kurš uzvarēja sporta notikumā numur 0?",
        "Cik punktus guva komanda?"])
    post = weekend.build_quiz(session, weekend.utcnow().date())
    assert post is not None
    # traģēdija nav ne jautājumos, ne receptes rakstu sarakstā
    grim = session.query(Article).filter_by(guid="q-grim").one()
    assert grim.id not in post.extra["recipe"]["articles"]
    # kvīza vākam ir foto (agrāk vāks bija tukšs krāsas laukums)
    assert seen["image"].startswith("https://cdn/uploads/")


def test_quiz_drops_a_grim_question_even_from_a_safe_article(session,
                                                             monkeypatch):
    for i in range(3):
        _art(session, f"qg-{i}", f"Notikums numur {i}")
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    monkeypatch.setattr(cards, "render_cards", lambda *a, **k: ["c0.png"])
    monkeypatch.setattr(weekend, "_ai_lines", lambda *a, **k: [
        "Cik cilvēku gāja bojā ugunsgrēkā?", "Kas notika 26. augustā?"])
    # paliek viens derīgs jautājums -> kvīzs netiek publicēts
    assert weekend.build_quiz(session, weekend.utcnow().date()) is None


def test_number_and_question_skip_grim_articles(session, monkeypatch):
    _art(session, "n-grim", "Slepkavība Rīgā: aizturēts vīrietis", sessions=9000)
    ok = _art(session, "n-ok", "Budžetā trūkst 47 miljoni eiro", sessions=500)
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    monkeypatch.setattr(cards, "render_number_card", lambda *a, **k: "n.png")
    monkeypatch.setattr(cards, "render_share_image", lambda *a, **k: "s.png")
    monkeypatch.setattr(weekend, "_ai_lines", lambda *a, **k: [
        "47 milj. €", "Tik daudz trūkst pašvaldību budžetos"])
    post = weekend.build_number(session, weekend.utcnow().date())
    assert post is not None and post.article_id == ok.id

    monkeypatch.setattr(weekend, "_ai_lines", lambda *a, **k: [
        "Vai budžeta samazinājums skars tavu pašvaldību?"])
    q = weekend.build_question(session, weekend.utcnow().date())
    assert q is not None and q.article_id == ok.id


# --- pārģenerēšana -------------------------------------------------------

def test_regenerate_redraws_a_digest_carousel_from_its_recipe(session,
                                                              monkeypatch):
    arts = [_art(session, f"rg-{i}", f"Notikums numur {i}", sessions=100 - i)
            for i in range(4)]
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    monkeypatch.setattr(cards, "render_cards",
                        lambda *a, **k: ["old0.png", "old1.png"])
    post = weekend.build_top5(session, weekend.utcnow().date(), None)
    assert post.media == ["old0.png", "old1.png"]
    assert post.extra["recipe"]["articles"] == [a.id for a in arts]

    seen = {}

    def new_cards(title, section, tag, points, image, question, **kwargs):
        seen.update(title=title, tag=tag, points=points, kwargs=kwargs)
        return [f"new{i}.png" for i in range(4)]

    monkeypatch.setattr(cards, "render_cards", new_cards)
    ok, message = regenerate.regenerate(session, post)
    assert ok and "pārģenerēta" in message
    assert post.media == [f"new{i}.png" for i in range(4)]
    assert post.extra["render_version"] == cards.RENDER_VERSION
    # tas pats izkārtojums: bez vāka, bez CTA, ar foto un datumiem
    assert seen["kwargs"]["include_cover"] is False
    assert len(seen["kwargs"]["point_images"]) == 4
    assert seen["points"] == [a.title for a in arts]


def test_regenerate_keeps_the_old_image_when_rendering_fails(session,
                                                             monkeypatch):
    a = _art(session, "rf-1", "Raksts")
    p = Post(article_id=a.id, channel="fb_tv3lv", format="photo", copy="x",
             media=["vecais.png"], state="scheduled",
             extra={"recipe": {"kind": "number", "number": "47%",
                               "context": "Konteksts", "article": a.id}})
    session.add(p)
    session.commit()

    def boom(*a, **k):
        raise RuntimeError("Chromium nokrita")

    monkeypatch.setattr(cards, "render_number_card", boom)
    ok, message = regenerate.regenerate(session, p)
    assert ok is False and "Chromium" in message
    assert p.media == ["vecais.png"]     # neizdevies mēģinājums nesabojā ierakstu


def test_regenerate_only_for_posts_that_have_not_gone_out(session):
    a = _art(session, "rp-1", "Raksts")
    published = Post(article_id=a.id, channel="fb_tv3lv", format="photo",
                     copy="x", media=["x.png"], state="published")
    session.add(published)
    session.commit()
    assert regenerate.can_regenerate(published) is False

    scheduled = Post(article_id=a.id, channel="fb_tv3lv", format="photo",
                     copy="x", media=["x.png"], state="scheduled")
    session.add(scheduled)
    session.commit()
    assert regenerate.can_regenerate(scheduled) is True


def test_preview_page_offers_the_button_and_the_route_works(client, session,
                                                            monkeypatch):
    client.post("/setup", data={"password": "slepens123", "password2": "slepens123"})
    a = _art(session, "pv-1", "Raksts ar attēlu")
    p = Post(article_id=a.id, channel="fb_tv3lv", format="photo", copy="Teksts",
             link_url=a.canonical_url, media=["vecais.png"], state="scheduled",
             scheduled_at=utcnow() + timedelta(hours=1))
    session.add(p)
    session.commit()

    r = client.get(f"/post/{p.id}/preview")
    assert r.status_code == 200 and "Pārģenerēt grafiku" in r.text

    import app.pipeline as pl

    monkeypatch.setattr(pl, "branded_photo",
                        lambda article, img, platform="": "jaunais.png")
    r = client.post(f"/post/{p.id}/regenerate", follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    session.expire_all()
    assert session.get(Post, p.id).media == ["jaunais.png"]
