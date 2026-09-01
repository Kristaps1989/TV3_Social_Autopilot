"""ES MI akta (Regula (ES) 2024/1689) 50. panta marķējums.

Marķējumam jābūt skaidram un pamanāmam. Tāpēc te tiek pārbaudīts ne tikai
tas, ka teksts kaut kur eksistē, bet ka tas tiešām aiziet līdz publicētajam
ierakstam: uz grafikas, parakstā un — ierunātās lentēs — arī skaļi.
"""
import pytest

from app import disclosure
from app.best_practices import assemble_post_text, sanitize_copy


def test_disclosure_is_on_without_anyone_configuring_it():
    """Noklusējums ir «marķēts». Regulas prasību nedrīkst nokārtot ar to, ka
    kāds aizmirsa ierakstīt atslēgu noteikumos."""
    assert disclosure.enabled({}) is True
    assert disclosure.text({}) == disclosure.DEFAULT_TEXT
    assert disclosure.short({}) == disclosure.DEFAULT_SHORT


def test_editors_can_reword_it_but_switching_it_off_is_explicit():
    assert disclosure.text({"ai_disclosure_text": "MI palīdzēja"}) == "MI palīdzēja"
    assert disclosure.text({"ai_disclosure": False}) == ""
    assert disclosure.badge_html({"ai_disclosure": False}) == ""


def test_x_gets_the_short_form():
    """280 zīmēs pilnais formulējums apēstu pašu ziņu."""
    long_form = disclosure.caption_line("facebook_page", {})
    short_form = disclosure.caption_line("x", {})
    assert short_form == disclosure.DEFAULT_SHORT
    assert len(short_form) < len(long_form)


def test_the_badge_carries_the_words_not_just_an_icon():
    html = disclosure.badge_html({})
    assert "MI" in html and disclosure.DEFAULT_SHORT in html


def test_room_for_the_disclosure_is_reserved_before_the_copy_is_cut():
    """Bez rezerves tvīts ar atrunu pārsniegtu limitu tieši tad, kad teksts
    ir garš — tātad visbiežāk."""
    copy = "Vārds " * 80
    note = disclosure.caption_line("x", {})
    trimmed, tags, _ = sanitize_copy(copy, [], "x", reserve_link_chars=True,
                                     reserve_chars=len(note) + 2)
    text = assemble_post_text(trimmed, tags, "https://tv3.lv/p/1", "x",
                              disclosure=note)
    # X saiti skaita kā 23 zīmes
    assert len(text) - len("https://tv3.lv/p/1") + 23 <= 280


def test_the_disclosure_is_the_last_line_of_the_caption():
    text = assemble_post_text("Teksts", ["#tv3"], "https://tv3.lv/p/1",
                              "facebook_page", disclosure="Veidots ar MI")
    assert text.rstrip().endswith("Veidots ar MI")
    assert text.index("https://tv3.lv/p/1") < text.index("Veidots ar MI")


def test_it_is_not_repeated_when_the_copy_already_says_it():
    assert disclosure.in_caption(
        "Šo apkopojumu Saturs sagatavots ar mākslīgā intelekta palīdzību", {})
    assert not disclosure.in_caption("Parasts paraksts par vētru", {})


def test_only_a_voiced_reel_carries_the_disclosure_in_its_caption(session):
    """Cauri visam ceļam, ne tikai palīgfunkcijā.

    Rakstu raksta žurnālists. Atruna zem KATRA ieraksta lasās kā apgalvojums,
    ka MI ir uzrakstījis rakstu, tāpēc noklusēti tā parādās tikai tur, kur
    tiešām ir mākslīgi ģenerēts medijs — lentē ar sintezēto balsi.
    """
    from app.models import Article, Post
    from app.pipeline import compose_text

    a = Article(guid="d-1", url="https://tv3.lv/d", canonical_url="https://tv3.lv/d",
                title="Ziņa", section="news", raw_json={})
    session.add(a)
    session.flush()
    post = Post(article_id=a.id, channel="fb_tv3lv", format="photo",
                copy="Kas notika Bauskas ielā", hashtags=[],
                link_url="https://tv3.lv/d", state="scheduled")
    session.add(post)
    session.flush()

    # foto ieraksts: attēls un teksts no žurnālista raksta -> bez atrunas
    text, _ = compose_text(post, "facebook_page", "https://tv3.lv/p/1")
    assert disclosure.DEFAULT_TEXT not in text

    # lente ar sintezēto balsi -> atruna ir
    post.format = "reel"
    post.extra = {"recipe": {"voiced": True}}
    session.flush()
    voiced, _ = compose_text(post, "facebook_page", "https://tv3.lv/p/1")
    assert disclosure.DEFAULT_TEXT in voiced

    # klusa lente: nekas nav ģenerēts skaņā -> bez atrunas
    post.extra = {"recipe": {"voiced": False}}
    session.flush()
    silent, _ = compose_text(post, "facebook_page", "https://tv3.lv/p/1")
    assert disclosure.DEFAULT_TEXT not in silent

    # plašākā interpretācija paliek pieejama redakcijai
    from app import config

    wide = dict(config.load_rules(), ai_disclosure_scope="all")
    everywhere, _ = compose_text(post, "facebook_page", "https://tv3.lv/p/1",
                                 rules=wide)
    assert disclosure.DEFAULT_TEXT in everywhere


def test_the_carousel_is_not_marked_unless_the_editor_widens_the_scope():
    """Karuselī nav ne balss, ne ģenerēta attēla — teksts ir no žurnālista
    raksta. Marķējums tur lasījās kā apgalvojums, ka MI ir uzrakstījis rakstu."""
    from app import cards

    def build(**kw):
        return cards.build_section_cards_html(
            "Virsraksts", "news", "#ZIŅAS",
            [{"title": "A", "body": "Pirmais teksts par notikumu."},
             {"title": "B", "body": "Otrais teksts par notikumu."}],
            [], "Ko tālāk?", **kw)

    plain = build()
    assert disclosure.DEFAULT_SHORT not in plain
    assert disclosure.DEFAULT_TEXT not in plain

    marked = build(ai_note=True)
    assert disclosure.DEFAULT_SHORT in marked    # zīmīte uz vāka
    assert disclosure.DEFAULT_TEXT in marked     # pilns teikums beigu kartītē


def test_missing_rules_are_reported_instead_of_silently_defaulted(tmp_path,
                                                                  monkeypatch):
    """Rediģējamā kopija tiek uzsēta vienu reizi; jauna atslēga uz jau
    strādājošas instances citādi nekad neparādās, un redaktors par to nezina."""
    from app import config

    editable = tmp_path / "editable"
    default = tmp_path / "default"
    editable.mkdir(), default.mkdir()
    (default / "rules.yaml").write_text("a: 1\nai_disclosure: true\n")
    (editable / "rules.yaml").write_text("a: 1\n")
    monkeypatch.setattr(config, "RULES_DIR", editable)
    monkeypatch.setattr(config, "DEFAULT_RULES_DIR", default)
    assert config.missing_rules() == ["ai_disclosure"]


def test_a_format_name_in_the_feeds_list_is_caught_on_save(monkeypatch):
    """«reel» kanāla `feeds:` sarakstā ir formāts, nevis plūsma.

    Klusi tas neko nelauza (kanāla `feeds:` kods pagaidām nelasa), un tieši
    tāpēc kļūda varēja nostāvēt nepamanīta. Saglabājot to tagad noķer.
    """
    from app import config

    bad = ("fb:\n  platform: facebook_page\n"
           "  feeds: [news_all, reel]\n  formats: [link, reel]\n")
    err = config.validate_editable("channels", bad)
    assert err and "formāts, nevis plūsma" in err


def test_unknown_feeds_formats_and_platforms_are_named(monkeypatch):
    from app import config

    base = ("fb:\n  platform: facebook_page\n"
            "  feeds: [news_all]\n  formats: [link]\n")
    assert config.validate_editable("channels", base) is None

    err = config.validate_editable("channels", base.replace("news_all", "ziņas"))
    assert err and "ziņas" in err and "neeksistē" in err

    err = config.validate_editable("channels", base.replace("[link]", "[link, reelz]"))
    assert err and "reelz" in err

    err = config.validate_editable("channels",
                                   base.replace("facebook_page", "fejsbuks"))
    assert err and "fejsbuks" in err
