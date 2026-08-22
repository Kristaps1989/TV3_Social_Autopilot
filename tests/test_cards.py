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
    assert "svg" in html_doc  # logo present
    # no sponsor right now -> no sponsor area by default
    assert "SADARBĪBĀ AR" not in html_doc


def test_sponsor_area_optional():
    html_doc = cards.build_cards_html("V", "news", "#T", ["a", "b", "c"], "", "q",
                                      show_sponsor=True)
    assert "SADARBĪBĀ AR" in html_doc


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
