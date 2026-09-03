"""Facebook saites kartīte ar MŪSU attēlu: augšai piesiets 1.91:1 griezums,
lai kartītē nenogriež galvas; noraidījums (domēns nav verificēts) neapdraud
ierakstu un tiek atcerēts."""
from datetime import timedelta

from app import config, credentials, pipeline
from app.models import Article, Post, utcnow


def _article(session, guid="lp-1", images=None):
    a = Article(guid=guid, url=f"https://tv3.lv/{guid}", canonical_url=f"https://tv3.lv/{guid}",
                title="Ostapenko pārceltajā mačā zaudē", section="sport",
                images=images or ["https://cdn/ostapenko.jpg"], raw_json={})
    session.add(a)
    session.flush()
    return a


def _post(session, a, fmt="link", extra=None):
    p = Post(article_id=a.id, channel="fb_lp", format=fmt, copy="c", hashtags=[],
             link_url=a.url, state="scheduled", extra=extra or {})
    session.add(p)
    session.flush()
    return p


FB = {"platform": "facebook_page", "formats": ["link", "photo"]}


def test_adapter_sends_picture_and_falls_back_when_facebook_rejects_it(session, monkeypatch):
    from adapters import facebook as fbmod
    from adapters.facebook import FacebookPageAdapter

    calls = []

    class Resp:
        def __init__(self, code, body):
            self.status_code, self._body = code, body
            self.text = str(body)

        def json(self):
            return self._body

    def fake_post(url, data=None, files=None, timeout=None):
        calls.append(dict(data))
        if "picture" in data and reject[0]:
            return Resp(400, {"error": {"message": "(#100) Only owners of the URL have the "
                                        "ability to specify the picture, name, thumbnail "
                                        "or description params.", "code": 100}})
        return Resp(200, {"id": "42_1"})

    monkeypatch.setattr(fbmod.httpx, "post", fake_post)
    adapter = FacebookPageAdapter()
    adapter.page_id, adapter.token = "42", "tok"

    reject = [False]
    assert adapter.publish(text="t", link="https://tv3.lv/a", images=[], fmt="link",
                           picture="https://app/media/crop.png") == "42_1"
    assert calls[-1]["picture"] == "https://app/media/crop.png"
    assert adapter.picture_rejected == ""

    # domēns nav verificēts: tas pats ieraksts aiziet bez attēla, iemesls paliek
    reject = [True]
    calls.clear()
    assert adapter.publish(text="t", link="https://tv3.lv/a", images=[], fmt="link",
                           picture="https://app/media/crop.png") == "42_1"
    assert [("picture" in c) for c in calls] == [True, False]
    assert "owners of the URL" in adapter.picture_rejected

    # bez saites `picture` nav ko sūtīt
    calls.clear()
    adapter.publish(text="t", link="", images=[], fmt="link", picture="https://x/y.png")
    assert "picture" not in calls[-1]


def test_link_picture_is_a_top_anchored_crop_rendered_before_publishing(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://autopilot.example")
    from app import cards, imageinfo

    a = _article(session)
    post = _post(session, a)
    monkeypatch.setattr(imageinfo, "image_size", lambda art, url: (1200, 900))  # 4:3 -> 30%
    rendered = {}

    def fake_crop(image, w, h, out_dir=None, contain=False, background="#fff",
                  position="center"):
        rendered.update(image=image, w=w, h=h, position=position)
        path = cards.CARDS_DIR / "crop_test.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"png")
        return str(path)

    monkeypatch.setattr(cards, "render_crop", fake_crop)
    url = pipeline.link_picture_for(session, post, FB)
    assert url == "https://autopilot.example/media/crop_test.png"
    assert rendered == {"image": "https://cdn/ostapenko.jpg", "w": 1200, "h": 628,
                        "position": cards.PHOTO_FOCUS}
    assert post.extra["link_picture"].endswith("crop_test.png")

    # otrreiz nezīmē no jauna
    rendered.clear()
    assert pipeline.link_picture_for(session, post, FB) == url
    assert rendered == {}

    # plats attēls: FB neko negriež, savs attēls nav vajadzīgs
    wide = _article(session, "lp-wide", ["https://cdn/plats.jpg"])
    monkeypatch.setattr(imageinfo, "image_size", lambda art, url: (1910, 1000))
    assert pipeline.link_picture_for(session, _post(session, wide), FB) == ""

    # tikai Facebook saites ierakstiem
    assert pipeline.link_picture_for(session, _post(session, a, fmt="photo"), FB) == ""
    assert pipeline.link_picture_for(session, _post(session, a),
                                     {"platform": "x", "formats": ["link"]}) == ""


def test_rejection_is_remembered_for_a_week_and_acceptance_keeps_links_as_links(
        session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    from app import imageinfo

    a = _article(session)
    post = _post(session, a)
    assert pipeline.link_picture_status(session) == "unknown"

    class Adapter:
        picture_rejected = "(#100) Only owners of the URL ..."

    pipeline.remember_link_picture_outcome(session, Adapter(), "https://x/crop.png", post)
    assert pipeline.link_picture_status(session) == "rejected"
    assert "owners" in post.extra["link_picture_rejected"]
    # noraidīts -> nemēģinām katru reizi
    assert pipeline.link_picture_for(session, post, FB) == ""
    # pēc nedēļas mēģinām atkal
    row = credentials.info(session, pipeline.LINK_PICTURE_KEY)
    row.updated_at = utcnow() - timedelta(days=8)
    session.commit()
    assert pipeline.link_picture_status(session) == "unknown"

    # pieņemts: portreta og attēls vairs nav iemesls pārslēgt uz photo
    monkeypatch.setattr(imageinfo, "orientation", lambda art: "portrait")
    monkeypatch.setattr(imageinfo, "image_size", lambda art, url: (800, 1000))
    assert pipeline.link_card_broken(session, "fb_lp", FB, a)[0] is True
    Adapter.picture_rejected = ""
    pipeline.remember_link_picture_outcome(session, Adapter(), "https://x/crop.png", post)
    assert pipeline.link_picture_status(session) == "ok"
    broken, loss = pipeline.link_card_broken(session, "fb_lp", FB, a)
    assert broken is False and loss > 0.5
    assert pipeline.retarget_queued_link_post(session, post, FB) is False

    # izslēgts noteikumos
    base = dict(config.load_rules())
    monkeypatch.setattr(config, "load_rules",
                        lambda: {**base, "link_card_custom_picture": False})
    assert pipeline.link_picture_status(session) == "off"
    assert pipeline.link_card_broken(session, "fb_lp", FB, a)[0] is True


def test_preview_explains_the_custom_picture(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://autopilot.example")
    from fastapi.testclient import TestClient

    from app import imageinfo
    from app.main import app

    monkeypatch.setattr(imageinfo, "image_size", lambda art, url: (1000, 1000))
    monkeypatch.setattr(imageinfo, "orientation", lambda art: "landscape")
    monkeypatch.setattr(config, "load_channels",
                        lambda: {"fb_lp": {**FB, "display_name": "FB"}})
    a = _article(session)
    post = _post(session, a)
    session.commit()
    client = TestClient(app)
    client.post("/setup", data={"password": "slepens123", "password2": "slepens123"})
    body = client.get(f"/post/{post.id}/preview").text
    assert "object-position:center 22%" in body
    assert "savu 1.91:1 griezumu" in body
    assert "automātiski kļūs par photo" not in body
