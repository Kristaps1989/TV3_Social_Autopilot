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
        "Cik punktus guva komanda?",
        "Kurā pilsētā notika sacensības?"])
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


# --- kvīza jautājumam jābūt par noslēgtu faktu ---------------------------

def test_open_ended_questions_are_rejected():
    assert weekend.open_ended(
        "Kas nepieciešams Latvijas basketbola izlasei, lai 28. augustā "
        "tiktu uz Pasaules kausa izcīņu?") == "nepiecieš"
    assert weekend.open_ended("Kurš uzvarēja spēlē 28. augustā?") == ""


def test_quiz_drops_questions_about_unfinished_situations(session, monkeypatch):
    for i in range(3):
        _art(session, f"oe-{i}", f"Sporta notikums numur {i}")
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    monkeypatch.setattr(cards, "render_cards", lambda *a, **k: ["c0.png"])
    monkeypatch.setattr(weekend, "_ai_lines", lambda *a, **k: [
        "Kas nepieciešams izlasei, lai tiktu uz Pasaules kausu?",
        "Vai komandai izdosies kvalificēties?",
        "Kurš guva visvairāk punktu 26. augustā?"])
    # paliek viens noslēgts jautājums -> kvīzs netiek publicēts
    assert weekend.build_quiz(session, weekend.utcnow().date()) is None


# --- vecie franšīžu ieraksti bez receptes -------------------------------

def test_old_franchise_post_without_a_recipe_can_be_rebuilt(session,
                                                            monkeypatch):
    """Ieraksti, kas tapa pirms receptēm, joprojām jāvar salabot — kvīzam
    pārbūve turklāt nozīmē JAUNUS jautājumus pēc pašreizējiem noteikumiem."""
    for i in range(3):
        _art(session, f"ob-{i}", f"Notikums numur {i}")
    old = Post(article_id=None, channel="fb_tv3lv", format="card_carousel",
               copy="Vecais kvīzs", link_url="https://tv3.lv",
               media=["vecais0.png", "vecais1.png"], hook_type="quiz",
               state="scheduled", scheduled_at=utcnow() + timedelta(hours=2),
               extra={"card_titles": ["x"]})
    a = _art(session, "ob-host", "Sintētiskais")
    old.article_id = a.id
    session.add(old)
    session.commit()
    assert regenerate.can_regenerate(old) is True

    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    monkeypatch.setattr(cards, "render_cards",
                        lambda *a, **k: ["jauns0.png", "jauns1.png"])
    monkeypatch.setattr(weekend, "_ai_lines", lambda *a, **k: [
        "Kurš uzvarēja 26. augustā?", "Cik punktus guva komanda?",
        "Kurā pilsētā notika spēle?"])
    when = old.scheduled_at
    ok, message = regenerate.regenerate(session, old)
    assert ok and "pārbūvēts" in message
    assert old.media == ["jauns0.png", "jauns1.png"]
    assert old.scheduled_at == when          # publicēšanas laiks nemainās
    assert old.extra["recipe"]["kind"] == "quiz"   # turpmāk ir recepte
    # pagaidu ieraksts netiek atstāts rindā
    quizzes = session.query(Post).filter_by(hook_type="quiz").all()
    assert len(quizzes) == 1


def test_cards_fall_back_to_a_blurred_photopost_graphic(session, monkeypatch):
    """Daudziem tv3.lv rakstiem cita attēla par photopost grafiku nav. Tīrā
    veidā to likt nedrīkst (dublētos virsraksts), bet izpludinātu — drīkst:
    teksts vairs nav salasāms, un kartīte nav plakans krāsas laukums."""
    arts = [_art(session, f"bl-{i}", f"Notikums numur {i}", sessions=100 - i)
            for i in range(3)]
    for a in arts[:2]:                      # tikai photopost grafika
        a.images = [f"https://cdn/photopost/{a.guid}.jpg"]
    session.commit()
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    seen = {}

    def fake_cards(title, section, tag, points, image, question, **kwargs):
        seen.update(kwargs)
        return ["c0.png", "c1.png", "c2.png"]

    monkeypatch.setattr(cards, "render_cards", fake_cards)
    post = weekend.build_top5(session, weekend.utcnow().date(), None)

    assert seen["point_images"][0] == ""            # photopost nav tīrs foto
    assert "photopost" in seen["point_blur"][0]     # bet der kā faktūra
    assert seen["point_images"][2].startswith("https://cdn/uploads/")
    assert seen["point_blur"][2] == ""              # tīram foto rezerve nav vajadzīga
    # priekšskatījums parāda, cik kartītēm sanāca īsts foto
    assert post.extra["recipe"]["photos"] == {"total": 3, "clean": 1, "blurred": 2}


def test_blurred_layer_hides_the_baked_in_headline():
    doc = cards.build_cards_html(
        "T", "news", "#TOP5", ["Punkts"], "", "",
        point_images=[""], point_blur=["https://cdn/photopost/g.jpg"],
        include_cover=False, include_end=False)
    assert "photopost/g.jpg" in doc and "blurbg" in doc
    assert "blur(30px)" in doc          # virsraksts izšķīst faktūrā


def test_quiz_needs_three_questions_and_short_ones(session, monkeypatch):
    for i in range(3):
        _art(session, f"q3-{i}", f"Notikums numur {i}")
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    monkeypatch.setattr(cards, "render_cards", lambda *a, **k: ["c0.png"])
    # divi derīgi jautājumi -> kvīzs izskatītos pēc pusfabrikāta, tāpēc nekā
    monkeypatch.setattr(weekend, "_ai_lines", lambda *a, **k: [
        "Kurš uzvarēja 26. augustā?", "Cik punktus guva komanda?"])
    assert weekend.build_quiz(session, weekend.utcnow().date()) is None
    # pārāk garš jautājums kartītē neietilpst -> ārā
    monkeypatch.setattr(weekend, "_ai_lines", lambda *a, **k: [
        "Kurš uzvarēja 26. augustā?", "Cik punktus guva komanda?",
        "Ko " + "ļoti gari paskaidrojot " * 8 + "atklāja pētījums?",
        "Kurā pilsētā notika spēle?"])
    post = weekend.build_quiz(session, weekend.utcnow().date())
    assert post is not None
    assert len(post.extra["recipe"]["questions"]) == 3
    assert all(len(q) <= 130 for q in post.extra["recipe"]["questions"])


def test_long_card_text_shrinks_instead_of_being_cut_off():
    short, long = "Kurš uzvarēja?", "Ko atklāja " + "gari " * 30 + "pētījums?"
    assert cards.fit_size(short, 54) == 54
    assert cards.fit_size(long, 54) < 40
    doc = cards.build_cards_html("T", "news", "#KVĪZS", [long], "", "J?")
    assert f"font-size:{cards.fit_size(long, 54)}px" in doc


def test_preview_lists_card_destinations_and_utm(client, session):
    client.post("/setup", data={"password": "slepens123", "password2": "slepens123"})
    a = _art(session, "ct-1", "Raksts")
    p = Post(article_id=a.id, channel="fb_tv3lv", format="card_carousel",
             copy="Teksts", link_url="https://tv3.lv", hook_type="quiz",
             media=["c0.png", "c1.png"], state="scheduled",
             scheduled_at=utcnow() + timedelta(hours=1),
             extra={"card_links": ["https://tv3.lv", a.canonical_url]})
    session.add(p)
    session.commit()
    r = client.get(f"/post/{p.id}/preview")
    assert r.status_code == 200
    assert "Kartīšu galamērķi" in r.text
    assert "quiz-karte2" in r.text and a.canonical_url in r.text
