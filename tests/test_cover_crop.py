"""Vāka kartīte nedrīkst nogriezt galvas: gatava grafika iet vesela uz
izpludināta fona, pašu foto griezums enkurots augšējā trešdaļā, un vākam
priekšroka platam tīram foto."""
from app import cards, imageinfo, pipeline
from app.models import Article

SECTIONS = [{"title": "Kas notika", "body": "Pirmais teksts ir gana garš, lai sadaļa derētu."},
            {"title": "Kas tālāk", "body": "Otrais teksts arī ir gana garš, lai sadaļa derētu."}]


def test_square_photopost_cover_is_framed_whole_not_cropped():
    doc = cards.build_section_cards_html(
        "Virsraksts", "news", "#TAGS", SECTIONS, [], "Jautājums?",
        cover_image="https://cdn/photopost/sq.jpg", cover_title=False,
        cover_fit="contain")
    cover = doc.split('<div class="card">')[1]
    assert "center/contain no-repeat" in cover           # vesela, ne griezta
    assert 'class="blurbg" style="background-image:url(https://cdn/photopost/sq.jpg)' in cover
    assert "center bottom/cover" not in cover

    # plata grafika joprojām iet pilnekrānā, griezta tikai sānos
    doc = cards.build_section_cards_html(
        "Virsraksts", "news", "#TAGS", SECTIONS, [], "Jautājums?",
        cover_image="https://cdn/photopost/wide.jpg", cover_title=False,
        cover_fit="cover")
    assert "center bottom/cover" in doc.split('<div class="card">')[1]


def test_own_title_cover_and_section_photos_anchor_to_the_upper_third():
    doc = cards.build_section_cards_html(
        "Virsraksts", "news", "#TAGS", SECTIONS, ["https://cdn/a.jpg", "https://cdn/b.jpg"],
        "Jautājums?", cover_image="https://cdn/clean.jpg", cover_title=True)
    assert f"url(https://cdn/clean.jpg) {cards.PHOTO_FOCUS}/cover" in doc
    assert f"url(https://cdn/a.jpg) {cards.PHOTO_FOCUS}/cover" in doc
    assert "center/cover" not in doc.replace("center/contain", "")

    points = cards.build_cards_html("V", "news", "#T", ["Viens", "Divi"],
                                    "https://cdn/photopost/sq.jpg", "Jautājums?",
                                    cover_title=False, cover_fit="contain")
    assert "center/contain no-repeat" in points


def _sizes(monkeypatch, table):
    monkeypatch.setattr(imageinfo, "image_size",
                        lambda article, url: table.get(url))


def test_cover_prefers_a_wide_clean_photo_over_a_square_graphic(monkeypatch):
    monkeypatch.setattr(pipeline.config, "load_rules",
                        lambda: {"photo_prefer_landscape": True,
                                 "prebranded_image_patterns": ["photopost"]})
    _sizes(monkeypatch, {"https://cdn/photopost/sq.jpg": (1080, 1080),
                         "https://cdn/wide.jpg": (1600, 900)})
    a = Article(guid="cc-1", url="u", canonical_url="u", title="T", section="news",
                images=["https://cdn/photopost/sq.jpg", "https://cdn/wide.jpg"])
    assert pipeline.photo_base_image(a) == "https://cdn/wide.jpg"
    # bez plata alternatīvas paliek kvadrāts — bet vākā tas iet vesels
    b = Article(guid="cc-2", url="u", canonical_url="u", title="T", section="news",
                images=["https://cdn/photopost/sq.jpg"])
    assert pipeline.photo_base_image(b) == "https://cdn/photopost/sq.jpg"
    assert pipeline.cover_fit_for(b, "https://cdn/photopost/sq.jpg") == "contain"


def test_cover_fit_depends_on_the_graphic_shape(monkeypatch):
    monkeypatch.setattr(pipeline.config, "load_rules",
                        lambda: {"prebranded_image_patterns": ["photopost"]})
    _sizes(monkeypatch, {"https://cdn/photopost/wide.jpg": (1200, 630),
                         "https://cdn/photopost/sq.jpg": (1080, 1080),
                         "https://cdn/clean.jpg": (800, 1000)})
    a = Article(guid="cc-3", url="u", canonical_url="u", title="T", section="news")
    assert pipeline.cover_fit_for(a, "https://cdn/photopost/wide.jpg") == "cover"
    assert pipeline.cover_fit_for(a, "https://cdn/photopost/sq.jpg") == "contain"
    assert pipeline.cover_fit_for(a, "https://cdn/photopost/unknown.jpg") == "contain"
    assert pipeline.cover_fit_for(a, "https://cdn/clean.jpg") == "cover"   # mūsu plāksne, enkurs


def test_resolve_format_passes_the_fit_to_the_renderer(session, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    _sizes(monkeypatch, {"https://cdn/photopost/sq.jpg": (1080, 1080)})
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    seen = {}

    def fake_render(*a, **kw):
        seen.update(kw)
        return ["data/cards/c1.png", "data/cards/c2.png"]

    monkeypatch.setattr(cards, "render_section_cards", fake_render)
    monkeypatch.setattr(pipeline, "section_backgrounds", lambda article: ([], ""))
    a = Article(guid="cc-4", url="https://tv3.lv/cc4", canonical_url="https://tv3.lv/cc4",
                title="T", section="news", images=["https://cdn/photopost/sq.jpg"])
    session.add(a)
    session.flush()
    cfg = {"formats": ["link", "card_carousel"], "platform": "facebook_page"}
    fmt, _m, recipe = pipeline.resolve_format(
        session, "fb_cc", cfg, a, {"format": "card_carousel", "card_sections": SECTIONS})
    assert fmt == "card_carousel"
    assert seen["cover_fit"] == "contain" and seen["cover_title"] is False
    assert recipe["cover_fit"] == "contain"
