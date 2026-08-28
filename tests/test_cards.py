import os
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app import cards
from app.models import Article, Post, utcnow
from app.pipeline import resolve_format, run_decisions


def test_build_html_contains_points_and_brand():
    html_doc = cards.build_cards_html(
        "Virsraksts", "news", "#EKONOMIKA",
        ["Pirmais punkts", "Otrais punkts", "Trešais punkts"],
        "", "Noslēguma jautājums?")
    assert html_doc.count('class="card"') == 5  # cover + 3 + end card
    for text in ("Pirmais punkts", "#EKONOMIKA", "ZIŅAS",
                 "Lasi pilno rakstu", "Noslēguma jautājums?"):
        assert text in html_doc
    assert "data:image/png;base64," in html_doc  # official logo embedded
    # no sponsor right now -> no sponsor area by default
    assert "SADARBĪBĀ AR" not in html_doc


def test_sponsor_area_optional():
    html_doc = cards.build_cards_html("V", "news", "#T", ["a", "b", "c"], "", "q",
                                      show_sponsor=True)
    assert "SADARBĪBĀ AR" in html_doc


def test_share_image_html():
    html_doc = cards.build_share_html("Virsraksts ar garumzīmēm āēī", "news",
                                      "https://tv3.lv/img.jpg", kicker="SKAIDROJUMS")
    assert "Virsraksts ar garumzīmēm āēī" in html_doc
    assert "SKAIDROJUMS" in html_doc
    assert "data:image/png;base64," in html_doc  # logo chip
    assert "#e3000f" in html_doc  # red accent


def test_branded_photo_falls_back_without_renderer(session, monkeypatch):
    from app import pipeline

    monkeypatch.setattr(cards, "renderer_available", lambda: False)
    a = Article(guid="bp-1", url="https://tv3.lv/bp", canonical_url="https://tv3.lv/bp",
                title="T", section="news")
    assert pipeline.branded_photo(a, "https://tv3.lv/img.jpg") == "https://tv3.lv/img.jpg"


def test_branded_photo_falls_back_on_error(session, monkeypatch):
    from app import pipeline

    monkeypatch.setattr(cards, "renderer_available", lambda: True)

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(cards, "render_share_image", boom)
    a = Article(guid="bp-2", url="https://tv3.lv/bp2", canonical_url="https://tv3.lv/bp2",
                title="T", section="news")
    assert pipeline.branded_photo(a, "https://tv3.lv/img.jpg") == "https://tv3.lv/img.jpg"


def test_fallback_copy_skips_duplicated_lead():
    from app.decide import fallback_decision
    from app.rules_engine import Verdict

    a = Article(guid="fc-1", url="https://tv3.lv/fc", canonical_url="https://tv3.lv/fc",
                title="Trešdaļa uzņēmumu par kiberuzbrukumiem nesatraucas",
                lead="Trešdaļa uzņēmumu par kiberuzbrukumiem nesatraucas, liecina aptauja.",
                section="news", editor_status="must")
    d = fallback_decision(a, {"fb_tv3lv": Verdict("eligible")})
    copy = d["channels"][0]["copy"]
    assert copy == a.title  # lead repeats the title -> not appended

    a.lead = "Pavisam cits ievads ar jaunu informāciju par notikumu."
    d = fallback_decision(a, {"fb_tv3lv": Verdict("eligible")})
    assert "Pavisam cits ievads" in d["channels"][0]["copy"]


def test_html_escapes_content():
    html_doc = cards.build_cards_html("<script>x</script>", "news", "#T",
                                      ["a", "b", "c"], "", "q")
    assert "<script>x</script>" not in html_doc


def _article(session, **kw):
    defaults = dict(guid=kw.pop("guid", "card-1"), url="https://tv3.lv/c",
                    canonical_url="https://tv3.lv/c", title="Skaidrojums par cenām",
                    section="news", editor_status="must",
                    published_at=utcnow() - timedelta(minutes=10),
                    images=[], labels=["ekonomika"])
    defaults.update(kw)
    a = Article(**defaults)
    session.add(a)
    session.commit()
    return a


CFG = {"formats": ["link", "photo", "card_carousel"], "platform": "facebook_page"}


def test_resolve_format_renders_carousel(session, monkeypatch):
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    monkeypatch.setattr(cards, "render_cards",
                        lambda *a, **k: ["data/cards/x0.png", "data/cards/x1.png"])
    a = _article(session)
    ch_dec = {"format": "card_carousel",
              "card_points": ["viens", "divi", "trīs"],
              "card_end_question": "Kas notiks tālāk?"}
    fmt, media = resolve_format(session, "fb_x", CFG, a, ch_dec)
    assert fmt == "card_carousel"
    assert len(media) == 2


def test_resolve_format_falls_back_without_points(session, monkeypatch):
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    a = _article(session, guid="card-2", url="https://tv3.lv/c2",
                 canonical_url="https://tv3.lv/c2")
    fmt, media = resolve_format(session, "fb_x", CFG, a,
                                {"format": "card_carousel", "card_points": ["tikai viens"]})
    assert fmt != "card_carousel"
    assert media == []


def test_resolve_format_falls_back_on_render_error(session, monkeypatch):
    monkeypatch.setattr(cards, "renderer_available", lambda: True)

    def boom(*a, **k):
        raise RuntimeError("no chromium")

    monkeypatch.setattr(cards, "render_cards", boom)
    a = _article(session, guid="card-3", url="https://tv3.lv/c3",
                 canonical_url="https://tv3.lv/c3")
    fmt, media = resolve_format(session, "fb_x", CFG, a,
                                {"format": "card_carousel",
                                 "card_points": ["a", "b", "c"]})
    assert fmt != "card_carousel"


@pytest.mark.skipif(not Path("/opt/pw-browsers/chromium-1194/chrome-linux/chrome").exists(),
                    reason="chromium not available")
def test_render_cards_real(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_CHROMIUM",
                       "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    paths = cards.render_cards("Tests", "sport", "#HOKEJS",
                               ["viens", "divi", "trīs"], "", "Jautājums?",
                               out_dir=tmp_path)
    assert len(paths) == 5
    for p in paths:
        assert os.path.getsize(p) > 10000  # real PNGs, not empty files


def test_render_failure_journal(tmp_path, monkeypatch):
    from app import cards

    monkeypatch.setattr(cards, "CARDS_DIR", tmp_path)
    assert cards.last_render_failure() == ""
    cards.record_render_failure("story", RuntimeError("Target crashed"))
    out = cards.last_render_failure()
    assert "story" in out and "Target crashed" in out and "RuntimeError" in out


def test_cards_dir_is_absolute():
    # Chromium opens renders via file:// URIs; Path.as_uri() raises on
    # relative paths, which silently killed every render in production
    from app import cards

    assert cards.CARDS_DIR.is_absolute()
