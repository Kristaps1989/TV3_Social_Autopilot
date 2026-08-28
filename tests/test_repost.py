from datetime import timedelta

from sqlalchemy import select

from app import config, pipeline
from app.models import Article, Post, utcnow


def _decision(*formats, score=0.9):
    """Aizstāj AI izsaukumu, bet dara to pašu, ko īstais decide(): pieraksta
    vērtējumu rakstam (no tā ir atkarīgs otrā viļņa slieksnis)."""
    payload = {"publish": True, "score": score, "reason": "spēcīgs stāsts",
               "labels": [], "sensitivity": [],
               "channels": [{"channel": "fb_tv3lv", "format": f,
                             "copy": f"Teksts ({f})", "hook_type": h}
                            for f, h in zip(formats, ("fact", "question", "number"))]}

    def fake_decide(article, verdicts, session):
        article.decided_at = utcnow()
        article.ai_score = score
        article.labels, article.sensitivity = [], []
        return payload

    return fake_decide


def _article(session, guid="rp-1"):
    a = Article(guid=guid, url=f"https://tv3.lv/{guid}",
                canonical_url=f"https://tv3.lv/{guid}",
                title="Spēcīgs stāsts ar attēlu", section="news",
                editor_status="must", images=["https://cdn/i.jpg"],
                published_at=utcnow() - timedelta(minutes=5))
    session.add(a)
    session.commit()
    return a


def _fb_only(monkeypatch, **overrides):
    """Tikai fb_tv3lv kanāls, bez klusajām stundām (tests iet arī naktī)."""
    cfg = dict(config.load_channels()["fb_tv3lv"])
    cfg.update(quiet_hours=[], min_gap_minutes=30, **overrides)
    monkeypatch.setattr(config, "load_channels", lambda: {"fb_tv3lv": cfg})
    return cfg


def _posts(session, article):
    return session.execute(
        select(Post).where(Post.article_id == article.id)
        .order_by(Post.scheduled_at)).scalars().all()


def test_strong_article_gets_a_second_wave_in_another_format(session, monkeypatch):
    _fb_only(monkeypatch, repost_after_minutes=60)
    monkeypatch.setattr(pipeline, "decide", _decision("photo", "link"))
    monkeypatch.setattr(pipeline, "branded_photo",
                        lambda article, img, platform="": "data/cards/x.png")
    a = _article(session)
    pipeline.run_decisions(session)

    posts = _posts(session, a)
    assert [p.format for p in posts] == ["photo", "link"]
    # otrais vilnis nāk ar konfigurēto nobīdi un ar citu tekstu/āķi
    gap = posts[1].scheduled_at - posts[0].scheduled_at
    assert timedelta(minutes=59) <= gap <= timedelta(minutes=75)
    assert posts[0].copy != posts[1].copy
    assert posts[0].hook_type != posts[1].hook_type


def test_second_wave_never_repeats_the_same_format(session, monkeypatch):
    """AI piesaka link divreiz: formātu izvēle otro pagriež uz citu formātu."""
    _fb_only(monkeypatch, repost_after_minutes=60)
    monkeypatch.setattr(pipeline, "decide", _decision("link", "link"))
    monkeypatch.setattr(pipeline, "branded_photo",
                        lambda article, img, platform="": "data/cards/x.png")
    a = _article(session, "rp-2")
    pipeline.run_decisions(session)
    formats = [p.format for p in _posts(session, a)]
    assert len(formats) == 2 and formats[0] == "link" and formats[1] != "link"


def test_second_wave_dropped_when_only_one_format_is_possible(session, monkeypatch):
    """Kanālam ar vienu formātu otrais vilnis būtu tikai dublikāts."""
    _fb_only(monkeypatch, repost_after_minutes=60, formats=["link"], format_mix={})
    monkeypatch.setattr(pipeline, "decide", _decision("link", "link"))
    a = _article(session, "rp-2b")
    pipeline.run_decisions(session)
    assert [p.format for p in _posts(session, a)] == ["link"]
    from app.models import Evaluation

    reasons = session.execute(
        select(Evaluation).where(Evaluation.article_id == a.id,
                                 Evaluation.outcome == "blocked")).scalars().all()
    assert any("atkārtojums tajā pašā formātā" in e.reason for e in reasons)


def test_weak_article_is_not_duplicated(session, monkeypatch):
    _fb_only(monkeypatch, repost_after_minutes=60)
    monkeypatch.setattr(pipeline, "decide", _decision("photo", "link", score=0.5))
    monkeypatch.setattr(pipeline, "branded_photo",
                        lambda article, img, platform="": "data/cards/x.png")
    a = _article(session, "rp-3")
    pipeline.run_decisions(session)
    assert len(_posts(session, a)) == 1


def test_channel_without_repost_config_stays_single(session, monkeypatch):
    _fb_only(monkeypatch, repost_after_minutes=0)
    monkeypatch.setattr(pipeline, "decide", _decision("photo", "link"))
    monkeypatch.setattr(pipeline, "branded_photo",
                        lambda article, img, platform="": "data/cards/x.png")
    a = _article(session, "rp-4")
    pipeline.run_decisions(session)
    assert len(_posts(session, a)) == 1


def test_repost_offset_helper():
    now = utcnow()
    existing = [Post(article_id=1, channel="c", format="photo", scheduled_at=now)]
    strong = Article(guid="g", url="u", canonical_url="u", title="T", section="news",
                     ai_score=0.9)
    weak = Article(guid="g2", url="u", canonical_url="u", title="T", section="news",
                   ai_score=0.4)
    assert pipeline.repost_offset(strong, {"repost_after_minutes": 60}, existing) == \
        now + timedelta(minutes=60)
    assert pipeline.repost_offset(weak, {"repost_after_minutes": 60}, existing) is None
    assert pipeline.repost_offset(strong, {}, existing) is None
    # jau divi ieraksti -> trešā nav
    assert pipeline.repost_offset(strong, {"repost_after_minutes": 60},
                                  existing * 2) is None


def test_misindented_channel_setting_is_caught_before_saving():
    """Nobīdīta rinda YAML pārvērš par atsevišķu kanālu — to noķeram
    saglabāšanas brīdī, un ielasīšana to izlaiž, nevis krīt."""
    bad = ("fb_tv3lv:\n"
           "  platform: facebook_page\n"
           "  format_mix: {link: 0.4}\n"
           "repost_after_minutes: 60\n")
    err = config.validate_editable("channels", bad)
    assert err and "repost_after_minutes" in err and "atkāpes" in err

    good = bad.replace("\nrepost_after_minutes", "\n  repost_after_minutes")
    assert config.validate_editable("channels", good) is None


def test_load_channels_survives_a_broken_entry(tmp_path, monkeypatch):
    (tmp_path / "channels.yaml").write_text(
        "fb_tv3lv:\n  platform: facebook_page\nrepost_after_minutes: 60\n",
        encoding="utf-8")
    monkeypatch.setattr(config, "RULES_DIR", tmp_path)
    assert list(config.load_channels()) == ["fb_tv3lv"]
