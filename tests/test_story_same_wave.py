"""Stāsts un lente vienā lēmuma vilnī: stāstam jāsaņem tikko uzbūvētā lente,
nevis statiskais attēls."""
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from app import cards, config, pipeline, reels
from app.models import Article, Post, utcnow


def test_story_decided_in_the_same_wave_reuses_the_fresh_reel(session, monkeypatch, tmp_path):
    reel_file = tmp_path / "reel_fresh.mp4"

    def fake_build_reel(*a, **kw):
        reel_file.write_bytes(b"mp4")
        kw.get("report", {})["voice_used"] = "voice"
        return str(reel_file)

    # rediģējamā kopija var būt vecāka par kodu — te vajag noklusējuma kanālus
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    monkeypatch.setattr(reels, "available", lambda: True)
    monkeypatch.setattr(reels, "build_reel", fake_build_reel)
    monkeypatch.setattr(reels, "media_duration", lambda p: 40.0)
    monkeypatch.setattr(reels, "has_voice", lambda post: True)
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    monkeypatch.setattr(cards, "render_story", lambda *a, **k: "data/cards/story_x.png")
    monkeypatch.setattr(pipeline, "section_backgrounds", lambda article: ([], ""))
    monkeypatch.setattr(pipeline, "unbranded_image", lambda article: "")

    decision = {"publish": True, "reason": "", "channels": [
        # stāsts apzināti PIRMAIS — order_channels lenti liek priekšā
        {"channel": "fb_stories", "format": "story", "copy": "S", "hook_type": "fact"},
        {"channel": "fb_tv3lv", "format": "reel", "copy": "R", "hook_type": "fact",
         "card_sections": [{"title": "Kas notika", "body": "Pirmais teksts ir gana garš, lai sadaļa derētu."},
                           {"title": "Kas tālāk", "body": "Otrais teksts arī ir gana garš, lai sadaļa derētu."}]},
    ]}
    monkeypatch.setattr(pipeline, "decide", lambda article, verdicts, session: decision)

    a = Article(guid="sw-1", url="https://tv3.lv/sw", canonical_url="https://tv3.lv/sw",
                title="Skaidrojums", section="news", editor_status="must",
                images=["https://tv3.lv/i.jpg"],
                published_at=utcnow() - timedelta(minutes=5))
    session.add(a)
    session.commit()
    pipeline.run_decisions(session)

    posts = {p.channel: p for p in session.execute(
        select(Post).where(Post.article_id == a.id)).scalars().all()}
    assert posts["fb_tv3lv"].format == "reel"
    assert posts["fb_tv3lv"].media == [str(reel_file)]
    assert posts["fb_stories"].format == "story"
    assert posts["fb_stories"].media == [str(reel_file)], posts["fb_stories"].media


def _fake_reel_env(monkeypatch, tmp_path, name="reel_later.mp4"):
    reel_file = tmp_path / name

    def fake_build_reel(*a, **kw):
        reel_file.write_bytes(b"mp4")
        return str(reel_file)

    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    monkeypatch.setattr(reels, "available", lambda: True)
    monkeypatch.setattr(reels, "build_reel", fake_build_reel)
    monkeypatch.setattr(reels, "media_duration", lambda p: 40.0)
    monkeypatch.setattr(reels, "has_voice", lambda post: True)
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    monkeypatch.setattr(cards, "render_story", lambda *a, **k: "data/cards/story_x.png")
    monkeypatch.setattr(pipeline, "section_backgrounds", lambda article: ([], ""))
    monkeypatch.setattr(pipeline, "unbranded_image", lambda article: "")
    return reel_file


def _article(session, guid):
    a = Article(guid=guid, url=f"https://tv3.lv/{guid}", canonical_url=f"https://tv3.lv/{guid}",
                title="Skaidrojums", section="news", editor_status="must",
                images=["https://tv3.lv/i.jpg"],
                published_at=utcnow() - timedelta(minutes=5))
    session.add(a)
    session.commit()
    return a


def test_manual_reel_made_later_is_taken_over_by_the_queued_story(session, monkeypatch, tmp_path):
    """Biežākais gadījums dzīvē: AI stāstu ieplāno ar attēlu, redaktors pēc
    tam lapā «Kāpēc» uztaisa lenti ar roku — stāstam jāpārņem lente."""
    from app import manual

    reel_file = _fake_reel_env(monkeypatch, tmp_path)
    decision = {"publish": True, "reason": "", "channels": [
        {"channel": "fb_stories", "format": "story", "copy": "S", "hook_type": "fact"}]}
    monkeypatch.setattr(pipeline, "decide", lambda article, verdicts, session: decision)
    a = _article(session, "ml-1")
    pipeline.run_decisions(session)
    story = session.execute(select(Post).where(Post.channel == "fb_stories",
                                               Post.article_id == a.id)).scalar_one()
    assert story.media == ["data/cards/story_x.png"]

    monkeypatch.setattr(manual, "_sections", lambda session, article, n: [
        {"title": "Kas notika", "body": "Pirmais teksts ir gana garš, lai sadaļa derētu."},
        {"title": "Kas tālāk", "body": "Otrais teksts arī ir gana garš, lai sadaļa derētu."}])
    monkeypatch.setattr(manual, "_voice", lambda session, article: "")
    post, msg = manual.build(session, a, "fb_tv3lv", "reel")
    assert post is not None and post.format == "reel", msg
    assert "stāsts pārņēma" in msg

    session.refresh(story)
    assert story.media == [str(reel_file)]
    assert story.extra.get("story_from_reel") is True
    assert story.state == "scheduled"


def test_published_story_is_left_alone_when_a_reel_appears(session, monkeypatch, tmp_path):
    reel_file = _fake_reel_env(monkeypatch, tmp_path)
    reel_file.write_bytes(b"mp4")
    a = _article(session, "pub-1")
    done = Post(article_id=a.id, channel="fb_stories", format="story", copy="",
                media=["data/cards/story_old.png"], state="published")
    reel = Post(article_id=a.id, channel="fb_tv3lv", format="reel", copy="",
                media=[str(reel_file)], state="scheduled")
    session.add_all([done, reel])
    session.commit()
    assert pipeline.upgrade_pending_stories(session, a) == 0
    assert done.media == ["data/cards/story_old.png"]


def test_story_takes_the_reel_at_publish_time_if_one_appeared(session, monkeypatch, tmp_path):
    """Pēdējais tīkls: pat ja neviens vilnis stāstu neatjaunoja, tieši pirms
    publicēšanas stāsts ar attēlu paskatās, vai rakstam nav lentes."""
    reel_file = _fake_reel_env(monkeypatch, tmp_path)
    reel_file.write_bytes(b"mp4")
    a = _article(session, "rt-1")
    img = Path("data/cards/story_rt.png")
    img.parent.mkdir(parents=True, exist_ok=True)
    img.write_bytes(b"png")
    story = Post(article_id=a.id, channel="fb_stories", format="story", copy="",
                 media=[str(img)], state="scheduled",
                 extra={"render_version": cards.RENDER_VERSION})
    reel = Post(article_id=a.id, channel="fb_tv3lv", format="reel", copy="",
                media=[str(reel_file)], state="scheduled")
    session.add_all([story, reel])
    session.commit()
    pipeline.refresh_missing_media(session, story, "facebook_page")
    assert story.media == [str(reel_file)]
    img.unlink(missing_ok=True)


def test_reel_lookup_sees_a_reel_added_after_the_posts_were_loaded(session, tmp_path):
    """`article.posts` ielādēta pirms lentes ieraksta to neredz — meklēšanai
    jāiet caur vaicājumu, citādi rokas režīmā lente stāstam paiet garām."""
    reel_file = tmp_path / "late.mp4"
    reel_file.write_bytes(b"mp4")
    a = _article(session, "stale-1")
    assert list(a.posts) == []                       # kolekcija ielādēta tukša
    session.add(Post(article_id=a.id, channel="fb_tv3lv", format="reel", copy="",
                     media=[str(reel_file)], state="scheduled"))
    session.flush()
    assert pipeline.article_reel_file(a, rules={"story_reuses_reel": True}) == str(reel_file)


def test_wiped_media_is_counted_for_the_startup_warning(session, tmp_path):
    a = _article(session, "wipe-1")
    here = tmp_path / "here.png"
    here.write_bytes(b"png")
    session.add_all([
        Post(article_id=a.id, channel="fb_tv3lv", format="photo", copy="",
             media=[str(here)], state="scheduled"),
        Post(article_id=a.id, channel="fb_stories", format="story", copy="",
             media=[str(tmp_path / "gone.png")], state="scheduled"),
        Post(article_id=a.id, channel="fb_tv3lv", format="link", copy="",
             media=["https://tv3.lv/i.jpg"], state="scheduled"),
        Post(article_id=a.id, channel="fb_tv3lv", format="reel", copy="",
             media=[str(tmp_path / "gone.mp4")], state="published"),
    ])
    session.commit()
    assert pipeline.wiped_media_count(session) == 1
