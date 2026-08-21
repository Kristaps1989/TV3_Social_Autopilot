from datetime import datetime

from app.formats import choose_format, suitable_formats
from app.models import Article, Post

NOW = datetime(2026, 8, 20, 10, 0)

CFG = {"formats": ["link", "photo", "photo_album", "text_only"],
       "format_weights": {"link": 1.0, "photo": 1.1, "photo_album": 0.9, "text_only": 0.4}}


def art(session=None, guid="f1", section="news", images=None, title="Virsraksts"):
    a = Article(guid=guid, url=f"https://tv3.lv/{guid}", canonical_url=f"https://tv3.lv/{guid}",
                title=title, section=section, images=images or [], first_seen_at=NOW)
    if session is not None:
        session.add(a)
        session.flush()
    return a


def fill_channel(session, channel, fmt, n):
    for i in range(n):
        a = art(session, guid=f"{channel}-{fmt}-{i}")
        session.add(Post(article_id=a.id, channel=channel, format=fmt,
                         copy="x", state="published", scheduled_at=NOW))
    session.flush()


def test_suitability_needs_assets():
    no_images = art()
    assert "photo" not in suitable_formats(no_images, CFG["formats"])
    gallery = art(images=["1", "2", "3", "4"])
    assert "photo_album" in suitable_formats(gallery, CFG["formats"])
    one_image = art(images=["1"])
    assert "photo" in suitable_formats(one_image, CFG["formats"])
    assert "photo_album" not in suitable_formats(one_image, CFG["formats"])


def test_link_saturation_switches_to_photo(session):
    """After a run of link posts, an image-carrying article should break
    the monotony with a photo post — this is the 'all posts are links' fix."""
    fill_channel(session, "fb_t", "link", 6)
    a = art(session, guid="new1", images=["img.jpg"])
    assert choose_format(session, "fb_t", CFG, a) == "photo"


def test_ai_choice_wins_when_feed_balanced(session):
    a = art(session, guid="new2", images=["img.jpg"])
    assert choose_format(session, "fb_t2", CFG, a, ai_choice="link") == "link"


def test_ai_choice_overridden_when_saturated(session):
    fill_channel(session, "fb_t3", "link", 6)
    a = art(session, guid="new3", images=["img.jpg"])
    assert choose_format(session, "fb_t3", CFG, a, ai_choice="link") == "photo"


def test_entertainment_with_gallery_prefers_album(session):
    a = art(session, guid="new4", section="entertainment",
            images=["1", "2", "3", "4", "5"])
    fmt = choose_format(session, "fb_t4", CFG, a)
    assert fmt in ("photo", "photo_album")


def test_no_images_stays_link(session):
    a = art(session, guid="new5")
    assert choose_format(session, "fb_t5", CFG, a) == "link"
