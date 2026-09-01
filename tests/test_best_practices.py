from app.best_practices import (
    add_utm,
    assemble_post_text,
    effective_length,
    pick_format,
    sanitize_copy,
    PLATFORM_SPECS,
)


def test_utm_params_added():
    url = add_utm("https://www.tv3.lv/raksts/abc", "facebook_page", 42)
    assert "utm_source=facebook_page" in url
    assert "utm_medium=social" in url
    assert "utm_campaign=autopilot" in url
    assert "utm_content=42" in url


def test_utm_preserves_existing_query():
    url = add_utm("https://tv3.lv/a?x=1", "x", 7)
    assert "x=1" in url and "utm_content=7" in url


def test_x_url_counts_as_23():
    spec = PLATFORM_SPECS["x"]
    text = "Īsa ziņa https://www.tv3.lv/very/long/url/that/is/definitely/longer/than/23"
    assert effective_length(text, spec) == len("Īsa ziņa ") + 23


def test_x_copy_fits_280_with_link():
    long_copy = "Ļoti gara ziņa par notikumu Latvijā. " * 20
    copy, tags, fixes = sanitize_copy(long_copy, [], "x", reserve_link_chars=True)
    # 280 - 24 (link) = 256 budget
    assert len(copy) <= 256
    assert any("truncated" in f for f in fixes)


def test_clickbait_removed():
    copy, _, fixes = sanitize_copy("Tu nenoticēsi, kas notika Rīgā!!", [], "facebook_page")
    assert "nenoticēsi" not in copy.lower()
    assert any("clickbait" in f for f in fixes)


def test_shouting_lowercased():
    copy, _, fixes = sanitize_copy("ŠOKĒJOŠI NOTIKUMI Rīgas centrā", [], "facebook_page")
    assert "NOTIKUMI" not in copy


def test_sober_tone_for_tragedy():
    copy, tags, fixes = sanitize_copy("Traģēdija Jūrmalā 😢 Gājis bojā cilvēks!",
                                      ["#jurmala"], "facebook_page",
                                      sensitivity=["tragedy"])
    assert "😢" not in copy
    assert "!" not in copy
    assert tags == []


def test_hashtag_limits_per_platform():
    _, tags_x, _ = sanitize_copy("Ziņa", ["a", "b", "c", "d"], "x")
    assert len(tags_x) == 2
    _, tags_fb, _ = sanitize_copy("Ziņa", ["a", "b"], "facebook_page")
    assert len(tags_fb) == 1
    assert all(t.startswith("#") for t in tags_x + tags_fb)


def test_max_one_question():
    copy, _, fixes = sanitize_copy("Kas notika? Kāpēc? Kurš vainīgs?", [], "threads")
    assert copy.count("?") == 1


def test_assemble_includes_link_in_copy_platforms():
    text = assemble_post_text("Ziņa", ["#tag"], "https://tv3.lv/a?utm_content=1", "x")
    assert "https://tv3.lv/a" in text and "#tag" in text


def test_assemble_excludes_link_for_instagram():
    text = assemble_post_text("Ziņa", [], "https://tv3.lv/a", "instagram")
    assert "https://" not in text


def test_pick_format_defaults():
    assert pick_format("news", [], "can", ["link", "photo"]) == "link"
    assert pick_format("entertainment", ["i1", "i2", "i3", "i4"], "can",
                       ["link", "photo", "photo_album"]) == "photo_album"
    assert pick_format("entertainment", ["i1"], "can", ["link", "photo"]) == "photo"
    assert pick_format("sport", ["i1"], "can", ["link", "text_only"]) == "link"


# --- attēlu apraksti (piekļūstamība) ---------------------------------------

def test_alt_text_describes_what_the_graphic_says():
    from app.best_practices import alt_text

    out = alt_text("Gaidāmas ekstremālas lietusgāzes un spēcīga vētra",
                   "news", "Justīne Jurcika")
    assert out.startswith("tv3.lv · news: ")
    assert "Gaidāmas ekstremālas lietusgāzes" in out
    assert "Justīne Jurcika" in out


def test_alt_text_without_optional_parts():
    from app.best_practices import alt_text

    assert alt_text("Virsraksts") == "tv3.lv: Virsraksts"
    assert alt_text("") == ""


def test_alt_text_is_capped_at_a_word_boundary():
    from app.best_practices import alt_text

    out = alt_text("vārds " * 100, "news", limit=80)
    assert len(out) <= 80 and out.endswith("…")


def test_publish_sends_alt_text(session, monkeypatch):
    """Katrs attēlu ieraksts aiziet ar aprakstu."""
    from datetime import timedelta

    from adapters import base as adapters_base
    from app import pipeline, runtime
    from app.models import Article, Post, utcnow

    a = Article(guid="alt-1", url="https://tv3.lv/a", canonical_url="https://tv3.lv/a",
                title="Vētra tuvojas Latvijai", section="news", raw_json={})
    session.add(a)
    session.flush()
    session.add(Post(article_id=a.id, channel="fb_tv3lv", format="photo",
                     copy="Teksts", link_url=a.canonical_url, media=["card.png"],
                     state="scheduled", dry_run=True,
                     scheduled_at=utcnow() - timedelta(minutes=1)))
    session.commit()

    seen = {}

    class Recorder(adapters_base.DryRunAdapter):
        def publish(self, **kwargs):
            seen.update(kwargs)
            return "dry-run"

    monkeypatch.setattr(pipeline, "get_adapter",
                        lambda platform: Recorder(platform))
    monkeypatch.setattr(runtime, "is_dry_run", lambda s: True)
    pipeline.publish_due(session)

    assert "Vētra tuvojas Latvijai" in seen.get("alt_text", "")
