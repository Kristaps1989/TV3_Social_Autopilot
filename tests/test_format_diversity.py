"""Karuselis un lente vairs nepārņem plūsmu: dienas kvota, saites grīda,
neveiksmes ar iemeslu un kvotu fakti promptā."""
from datetime import timedelta

from sqlalchemy import select

from app import cards, config, decide, pipeline, reels
from app.models import Article, Evaluation, Post, utcnow

CFG = {"platform": "facebook_page", "formats": ["link", "photo", "card_carousel", "reel"],
       "format_mix": {"link": 0.4}, "format_daily_cap": {"card_carousel": 2, "reel": 1}}
SECTIONS = [{"title": "Kas notika", "body": "Pirmais teksts ir gana garš, lai sadaļa derētu."},
            {"title": "Kas tālāk", "body": "Otrais teksts arī ir gana garš, lai sadaļa derētu."}]


def _article(session, guid="fd-1"):
    a = Article(guid=guid, url=f"https://tv3.lv/{guid}", canonical_url=f"https://tv3.lv/{guid}",
                title="Skaidrojums par vēju", section="news", editor_status="must",
                images=["https://tv3.lv/i.jpg"],
                published_at=utcnow() - timedelta(minutes=5))
    session.add(a)
    session.flush()
    return a


def _post(session, article, fmt, channel="fb_q", when=None):
    p = Post(article_id=article.id, channel=channel, format=fmt, copy="x",
             state="scheduled", scheduled_at=when or utcnow())
    session.add(p)
    session.flush()
    return p


def _fake_carousel(monkeypatch):
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    monkeypatch.setattr(cards, "render_section_cards",
                        lambda *a, **k: ["data/cards/c1.png", "data/cards/c2.png"])
    monkeypatch.setattr(pipeline, "section_backgrounds", lambda article: ([], ""))
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)


def test_daily_quota_turns_the_third_carousel_into_a_link(session, monkeypatch):
    _fake_carousel(monkeypatch)
    a = _article(session)
    # kvota brīva un saites grīda izpildīta -> karuselis
    for fmt in ("link", "link", "link", "photo"):
        _post(session, a, fmt, when=utcnow() - timedelta(days=2))
    notes: list[str] = []
    fmt, media, _ = pipeline.resolve_format(session, "fb_q", CFG, a,
                                            {"format": "card_carousel", "card_sections": SECTIONS},
                                            notes=notes)
    assert fmt == "card_carousel" and notes == []

    _post(session, a, "card_carousel")
    _post(session, a, "card_carousel")
    for fmt in ("link", "link", "link"):
        _post(session, a, fmt, when=utcnow() - timedelta(days=2))
    notes = []
    fmt, media, _ = pipeline.resolve_format(session, "fb_q", CFG, a,
                                            {"format": "card_carousel", "card_sections": SECTIONS},
                                            notes=notes)
    assert fmt != "card_carousel"
    assert notes and "kvota 2/2" in notes[0]
    # rokas režīms kvotu neskata
    fmt, _m, _r = pipeline.resolve_format(session, "fb_q", CFG, a,
                                          {"format": "card_carousel", "card_sections": SECTIONS},
                                          enforce=False)
    assert fmt == "card_carousel"


def test_link_floor_beats_an_ai_carousel(session, monkeypatch):
    _fake_carousel(monkeypatch)
    a = _article(session, "fd-2")
    for _ in range(6):
        _post(session, a, "card_carousel", when=utcnow() - timedelta(days=3))
    notes: list[str] = []
    fmt, _m, _r = pipeline.resolve_format(session, "fb_q", CFG, a,
                                          {"format": "card_carousel", "card_sections": SECTIONS},
                                          notes=notes)
    assert fmt == "link"
    assert any("saites grīda" in n for n in notes)


def test_reel_failure_is_recorded_and_explained(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    monkeypatch.setattr(reels, "available", lambda: True)

    def boom(*a, **kw):
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr(reels, "build_reel", boom)
    monkeypatch.setattr(pipeline, "section_backgrounds", lambda article: ([], ""))
    monkeypatch.setattr(pipeline, "unbranded_image", lambda article: "")
    a = _article(session, "fd-3")
    for fmt in ("link", "link", "link", "photo"):
        _post(session, a, fmt, when=utcnow() - timedelta(days=2))
    notes: list[str] = []
    fmt, _m, _r = pipeline.resolve_format(session, "fb_q", CFG, a,
                                          {"format": "reel", "card_sections": SECTIONS},
                                          notes=notes)
    assert fmt in ("link", "photo")
    assert any("lentes būve neizdevās" in n and "ffmpeg exploded" in n for n in notes)
    assert "reel" in cards.last_render_failure()

    monkeypatch.setattr(reels, "available", lambda: False)
    notes = []
    pipeline.resolve_format(session, "fb_q", CFG, a,
                            {"format": "reel", "card_sections": SECTIONS}, notes=notes)
    assert any("nav pieejams" in n for n in notes)


def test_the_wave_records_why_the_ai_format_was_not_used(session, monkeypatch):
    _fake_carousel(monkeypatch)
    a = _article(session, "fd-4")
    for _ in range(6):
        _post(session, a, "card_carousel", channel="fb_tv3lv",
              when=utcnow() - timedelta(days=3))
    decision = {"publish": True, "reason": "", "channels": [
        {"channel": "fb_tv3lv", "format": "card_carousel", "copy": "C", "hook_type": "fact",
         "card_sections": SECTIONS}]}
    monkeypatch.setattr(pipeline, "decide", lambda article, verdicts, session: decision)
    b = Article(guid="fd-5", url="https://tv3.lv/fd5", canonical_url="https://tv3.lv/fd5",
                title="Jauna ziņa", section="news", editor_status="must",
                images=["https://tv3.lv/i.jpg"], published_at=utcnow() - timedelta(minutes=5))
    session.add(b)
    session.commit()
    pipeline.run_decisions(session)
    post = session.execute(select(Post).where(Post.article_id == b.id,
                                              Post.channel == "fb_tv3lv")).scalar_one()
    assert post.format == "link"
    assert any("saites grīda" in n for n in post.extra["format_notes"])
    ev = session.execute(select(Evaluation).where(Evaluation.article_id == b.id,
                                                  Evaluation.outcome == "posted")).scalar_one()
    assert "formāts: card_carousel" in ev.reason


def test_prompt_states_todays_quotas_as_facts(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    a = _article(session, "fd-6")
    _post(session, a, "card_carousel", channel="fb_tv3lv")
    _post(session, a, "card_carousel", channel="fb_tv3lv")
    cfg = config.load_channels()
    text = decide.format_quota_context(session, ["fb_tv3lv"], cfg)
    assert "2/2 card_carousel" in text and "0/2 reel" in text
    assert "nepiedāvā: card_carousel" in text
    assert "saites daļa" in text
