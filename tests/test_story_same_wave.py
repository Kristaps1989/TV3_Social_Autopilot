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
