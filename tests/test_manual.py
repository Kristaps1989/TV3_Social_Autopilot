"""Redaktora rokas vadība: uztaisīt reelu/karuseli/foto konkrētam rakstam."""
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app import cards, manual, reels
from app.main import app
from app.models import Article, Post, utcnow


# Kanālu konfigurācija testam: rediģējamā kopija uz katras mašīnas ir cita
# (data/ ir gitignorēts), tāpēc testi to nedrīkst lasīt.
CHANNELS = {
    "fb_tv3lv": {"platform": "facebook_page", "min_gap_minutes": 45,
                 "daily_cap": 0, "quiet_hours": [], "sections": [],
                 "formats": ["link", "photo", "photo_album",
                             "card_carousel", "reel"]},
    "x_tv3zinas": {"platform": "x", "min_gap_minutes": 15, "daily_cap": 0,
                   "quiet_hours": [], "sections": [],
                   "formats": ["link", "photo", "text_only"]},
}


@pytest.fixture(autouse=True)
def channels(monkeypatch):
    from app import config as config_mod

    monkeypatch.setattr(config_mod, "load_channels", lambda: dict(CHANNELS))
    return CHANNELS


@pytest.fixture()
def client(session):
    with TestClient(app) as c:
        c.post("/setup", data={"password": "slepens123", "password2": "slepens123"})
        yield c


def _article(session, guid="m-1", body=True, images=True, status="can"):
    raw = {}
    if body:
        raw["_page_meta"] = {
            "body": ("Namā Bauskas ielā 15 daļēji iebruka jumts pēc gāzes "
                     "sprādziena. Pašvaldība vēl nav pieņēmusi lēmumu par ēkas "
                     "nākotni, un eksperti turpina vērtēt konstrukcijas."),
            "tags": ["Rīga"]}
    a = Article(guid=guid, url=f"https://tv3.lv/{guid}",
                canonical_url=f"https://tv3.lv/{guid}",
                title="Kas zināms par Bauskas ielas namu", section="news",
                lead="Deviņus mēnešus pēc sprādziena.", editor_status=status,
                images=["https://cdn/uploads/foto.jpg"] if images else [],
                ai_score=0.5, raw_json=raw)
    session.add(a)
    session.flush()
    return a


def _fake_points(monkeypatch, lines=(
        "Jumts daļēji iebruka | Sprādziena vilnis norāva daļu jumta un "
        "izsita logus trīs stāvos. Nams pagaidām nav apdzīvojams.",
        "Lēmums vēl nav pieņemts | Pašvaldība joprojām vērtē ēkas nākotni. "
        "Eksperti turpina mērīt konstrukciju noturību.",
        "Iedzīvotāji gaida atbildes | Daļa dzīvokļu īpašnieku joprojām dzīvo "
        "pie radiem un gaida pašvaldības lēmumu.")):
    from app import weekend

    monkeypatch.setattr(weekend, "_ai_lines", lambda *a, **k: list(lines))


def test_options_follow_the_channel_config():
    fb = {"formats": ["link", "photo", "photo_album", "card_carousel", "reel"]}
    assert manual.options(fb) == ["reel", "card_carousel", "photo",
                                  "photo_album", "link"]
    # kanāls bez lentēm tādu nepiedāvā
    assert "reel" not in manual.options({"formats": ["link", "photo"]})
    assert manual.options({}) == []


def test_unavailable_names_formats_no_channel_accepts(monkeypatch):
    from app import config as config_mod

    # tieši šis bija iemesls, kāpēc reeli neparādījās: konfigurācijā to nav
    monkeypatch.setattr(config_mod, "load_channels", lambda: {
        "fb_tv3lv": {"formats": ["link", "photo", "card_carousel"]}})
    assert "reel" in manual.unavailable()
    monkeypatch.setattr(config_mod, "load_channels", lambda: dict(CHANNELS))
    assert "reel" not in manual.unavailable()


def test_editor_can_ask_for_a_reel(session, monkeypatch):
    _fake_points(monkeypatch)
    monkeypatch.setattr(reels, "available", lambda: True)
    monkeypatch.setattr(reels, "build_reel",
                        lambda *a, **k: "/data/cards/reel_manual.mp4")
    article = _article(session)
    session.commit()

    post, msg = manual.build(session, article, "fb_tv3lv", "reel")
    assert post is not None, msg
    assert post.format == "reel"
    assert post.state == "scheduled" and post.scheduled_at is not None
    assert post.media == ["/data/cards/reel_manual.mp4"]
    assert (post.extra or {}).get("manual") is True
    # sadaļas nāk no raksta teksta un paliek receptē pārzīmēšanai
    assert post.extra["recipe"]["sections"][0]["title"] == "Jumts daļēji iebruka"
    assert "Sprādziena vilnis" in post.extra["recipe"]["sections"][0]["body"]


def test_manual_post_shows_up_in_the_decision_history(session, monkeypatch):
    from sqlalchemy import select

    from app.models import Evaluation

    _fake_points(monkeypatch)
    monkeypatch.setattr(reels, "available", lambda: True)
    monkeypatch.setattr(reels, "build_reel", lambda *a, **k: "/data/r.mp4")
    article = _article(session)
    session.commit()
    manual.build(session, article, "fb_tv3lv", "reel")

    reasons = session.execute(
        select(Evaluation.reason).where(Evaluation.article_id == article.id)
    ).scalars().all()
    assert any("redaktora pieprasīts reel" in r for r in reasons)


def test_photo_needs_no_ai(session, monkeypatch):
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    monkeypatch.setattr("app.pipeline.branded_photo",
                        lambda a, img, platform="": "/data/cards/photo.png")
    article = _article(session, "m-photo", body=False)
    session.commit()

    post, msg = manual.build(session, article, "fb_tv3lv", "photo")
    assert post is not None, msg
    assert post.format == "photo" and post.media == ["/data/cards/photo.png"]


def test_refuses_a_format_the_channel_does_not_take(session):
    article = _article(session, "m-x")
    session.commit()
    post, msg = manual.build(session, article, "x_tv3zinas", "reel")
    assert post is None and "nepieņem" in msg


def test_refuses_when_the_editor_said_dont(session):
    article = _article(session, "m-dont", status="dont")
    session.commit()
    post, msg = manual.build(session, article, "fb_tv3lv", "photo")
    assert post is None and "dont" in msg


def test_refuses_a_reel_without_enough_points(session, monkeypatch):
    from app import weekend

    monkeypatch.setattr(weekend, "_ai_lines",
                        lambda *a, **k: ["Viens virsraksts bez teksta"])
    monkeypatch.setattr(reels, "available", lambda: True)
    article = _article(session, "m-thin")
    session.commit()

    post, msg = manual.build(session, article, "fb_tv3lv", "reel")
    assert post is None
    assert "2 kartīšu sadaļas" in msg


def test_says_so_when_the_renderer_is_missing(session, monkeypatch):
    _fake_points(monkeypatch)
    monkeypatch.setattr(reels, "available", lambda: False)
    monkeypatch.setattr(cards, "renderer_available", lambda: False)
    article = _article(session, "m-norender")
    session.commit()

    post, msg = manual.build(session, article, "fb_tv3lv", "reel")
    assert post is None
    assert "ffmpeg" in msg or "renderētājs" in msg


def test_reuses_the_copy_the_ai_already_wrote(session, monkeypatch):
    _fake_points(monkeypatch)
    monkeypatch.setattr(reels, "available", lambda: True)
    monkeypatch.setattr(reels, "build_reel", lambda *a, **k: "/data/r.mp4")
    article = _article(session, "m-copy")
    session.add(Post(article_id=article.id, channel="fb_tv3lv", format="link",
                     copy="AI uzrakstīts teksts par namu", state="published",
                     published_at=utcnow()))
    session.commit()

    post, _ = manual.build(session, article, "fb_tv3lv", "reel")
    assert post.copy.startswith("AI uzrakstīts teksts")


def test_article_page_offers_the_buttons_and_the_route_works(client, session,
                                                             monkeypatch):
    _fake_points(monkeypatch)
    monkeypatch.setattr(reels, "available", lambda: True)
    monkeypatch.setattr(reels, "build_reel", lambda *a, **k: "/data/r.mp4")
    article = _article(session, "m-web")
    session.commit()

    page = client.get(f"/why?url={article.canonical_url}").text
    assert "Uztaisīt formātu ar roku" in page
    assert f"/article/{article.id}/make" in page
    assert "reel" in page

    r = client.post(f"/article/{article.id}/make",
                    data={"channel": "fb_tv3lv", "fmt": "reel"},
                    follow_redirects=False)
    location = unquote(r.headers["location"])
    assert "ok=1" in location and "reel" in location

    from sqlalchemy import select

    made = session.execute(
        select(Post).where(Post.article_id == article.id)).scalars().all()
    assert [p.format for p in made] == ["reel"]


def test_route_reports_the_reason_when_it_cannot(client, session, monkeypatch):
    monkeypatch.setattr(reels, "available", lambda: False)
    monkeypatch.setattr(cards, "renderer_available", lambda: False)
    article = _article(session, "m-fail")
    session.commit()

    r = client.post(f"/article/{article.id}/make",
                    data={"channel": "fb_tv3lv", "fmt": "reel"},
                    follow_redirects=False)
    assert "ok=0" in unquote(r.headers["location"])


def test_the_post_history_shows_the_format_and_a_preview(client, session,
                                                         monkeypatch):
    """Vēsturē ierakstu atrod pēc formāta un bildītes, ne pēc teksta sākuma."""
    from app import main as main_mod

    article = _article(session, "m-history")
    session.add_all([
        Post(article_id=article.id, channel="fb_tv3lv", format="reel",
             copy="Lentes teksts", state="published", media=["/data/r.mp4"],
             published_at=utcnow()),
        Post(article_id=article.id, channel="x_tv3zinas", format="photo",
             copy="Foto teksts", state="cancelled",
             media=["https://cdn/uploads/foto.jpg"]),
    ])
    session.commit()
    monkeypatch.setattr(main_mod, "_post_thumb",
                        lambda p: ({"src": "/media/r.mp4", "video": True}
                                   if p.format == "reel"
                                   else {"src": p.media[0], "video": False}))

    page = client.get(f"/why?url={article.canonical_url}").text
    assert "<th>Formāts</th>" in page and "<th>Priekšskatījums</th>" in page
    assert ">reel<" in page and ">photo<" in page
    assert "/media/r.mp4#t=0.5" in page
    # abiem ierakstiem ceļš uz pilno priekšskatījumu
    ids = [p.id for p in article.posts]
    assert all(f"/post/{i}/preview" in page for i in ids)


def test_a_reel_whose_file_is_gone_still_shows_up_in_the_history(client,
                                                                 session):
    """Konteiners failus nesaglabā — vecs ieraksts nedrīkst pazust no vēstures."""
    article = _article(session, "m-gone")
    session.add(Post(article_id=article.id, channel="fb_tv3lv", format="reel",
                     copy="Vecā lente", state="published",
                     media=["/data/cards/nekad-nebijis.mp4"],
                     published_at=utcnow()))
    session.commit()

    page = client.get(f"/why?url={article.canonical_url}").text
    assert "Vecā lente" in page and ">reel<" in page
    assert "fails vairs nav" in page


def test_the_thumbnail_points_at_the_file_that_is_still_there(tmp_path,
                                                              monkeypatch):
    from app import cards
    from app.main import _post_thumb

    monkeypatch.setattr(cards, "CARDS_DIR", tmp_path)
    (tmp_path / "lente.mp4").write_bytes(b"mp4")

    assert _post_thumb(Post(format="reel", media=["/data/cards/lente.mp4"])) \
        == {"src": "/media/lente.mp4", "video": True}
    assert _post_thumb(Post(format="photo",
                            media=["https://cdn/uploads/foto.jpg"])) \
        == {"src": "https://cdn/uploads/foto.jpg", "video": False}
    assert _post_thumb(Post(format="text_only", media=[])) == {}
