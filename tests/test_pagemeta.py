from pathlib import Path

from app import pagemeta, shortlinks
from app.models import Article

FIXTURES = Path(__file__).parent / "fixtures"
PAGE = (FIXTURES / "article_page.html").read_text(encoding="utf-8")
URL = "https://tv3.lv/zinas/latvija/spradziens-atnema-adelei-pirmo-dzivokli/"


def make_article(session, **kwargs):
    article = Article(guid=kwargs.pop("guid", "g1"), url=URL, canonical_url=URL,
                      title="Kas zināms par Bauskas ielas namu?", **kwargs)
    session.add(article)
    session.flush()
    return article


def test_parse_datalayer():
    meta = pagemeta.parse(PAGE)
    assert meta["post_id"] == "3879950"
    assert meta["author"] == "Gundega Gaujere"
    assert meta["tags"][:2] == ["Bauskas iela", "Gāzes sprādziens"]
    assert meta["categories"] == ["Ziņas", "Latvija", "Sabiedrība"]
    assert meta["post_types"] == ["video", "gallery"]
    assert meta["label"] == "Tikai tv3.lv"
    assert meta["content_chars"] == 11587
    assert meta["publish_date"] == "2026-08-31"


def test_parse_survives_js_object():
    """The dataLayer blob is JavaScript, not JSON: single quotes and a
    trailing comma must not cost us the whole page."""
    html = """<script>var dlEvent = {"Post ID":42,"Editor name":"Anna Bērziņa",
              "Tags":"Rīga;Vēlēšanas", 'x': undefined,};dataLayer.push(dlEvent);</script>"""
    meta = pagemeta.parse(html)
    assert meta["post_id"] == "42"
    assert meta["author"] == "Anna Bērziņa"
    assert meta["tags"] == ["Rīga", "Vēlēšanas"]


def test_parse_meta_tag_fallback():
    html = ('<link rel="shortlink" href="https://tv3.lv/?p=771">'
            '<meta property="article:author" content="Jānis Ozols">')
    meta = pagemeta.parse(html)
    assert meta["post_id"] == "771"
    assert meta["author"] == "Jānis Ozols"


def test_parse_empty_page():
    assert pagemeta.parse("<html><body>nekā</body></html>") == {}
    assert pagemeta.parse("") == {}


def test_enrich_caches_and_fetches_once(session, monkeypatch):
    article = make_article(session)
    calls = []
    monkeypatch.setattr(pagemeta, "fetch", lambda url, timeout=10: calls.append(url) or PAGE)

    pagemeta.enrich(article)
    assert calls == [URL]
    assert pagemeta.author(article) == "Gundega Gaujere"

    pagemeta.enrich(article)          # already cached -> no second request
    assert calls == [URL]
    pagemeta.enrich(article, force=True)
    assert calls == [URL, URL]


def test_enrich_failure_does_not_retry_immediately(session, monkeypatch):
    article = make_article(session)
    calls = []
    monkeypatch.setattr(pagemeta, "fetch", lambda url, timeout=10: calls.append(url) or "")

    assert pagemeta.enrich(article) == {}
    pagemeta.enrich(article)
    assert len(calls) == 1                       # backoff holds
    assert (article.raw_json or {}).get("_page_meta_at")


def test_enrich_off_by_rules(session, monkeypatch):
    article = make_article(session)
    monkeypatch.setattr(pagemeta, "fetch",
                        lambda url, timeout=10: _must_not_fetch())
    assert pagemeta.enrich(article, rules={"page_meta": False}) == {}


def _must_not_fetch():  # pragma: no cover — only runs if the flag is ignored
    raise AssertionError("page must not be fetched when page_meta is off")


def test_short_url_and_tracked_query(session, monkeypatch):
    article = make_article(session)
    monkeypatch.setattr(pagemeta, "fetch", lambda url, timeout=10: PAGE)
    pagemeta.enrich(article)

    assert pagemeta.short_url(article) == "https://tv3.lv/p/3879950"
    tracked = pagemeta.tracked_short_url(
        article, "https://tv3.lv/zinas/x/?utm_source=facebook_page&utm_content=7")
    assert tracked == ("https://tv3.lv/p/3879950"
                       "?utm_source=facebook_page&utm_content=7")


def test_short_url_without_metadata(session):
    assert pagemeta.short_url(make_article(session)) == ""


def test_display_link_prefers_own_code_then_cms(session, monkeypatch):
    article = make_article(session)
    monkeypatch.setattr(pagemeta, "fetch", lambda url, timeout=10: PAGE)
    pagemeta.enrich(article)
    tracked = "https://tv3.lv/zinas/x/?utm_source=facebook_page"

    # off by default: the full tracked URL still goes out
    assert shortlinks.display_link(5, tracked, {}, article) == tracked
    # enabled: the CMS short link, UTM tail intact
    assert shortlinks.display_link(5, tracked, {"cms_short_links": True},
                                   article).startswith("https://tv3.lv/p/3879950?")
    # our own /r/ code counts clicks, so it stays first
    own = shortlinks.display_link(5, tracked, {"short_link_base": "https://tv3.lv/r",
                                               "cms_short_links": True}, article)
    assert own == "https://tv3.lv/r/" + shortlinks.encode(5)


def test_hashtags_from_editorial_tags(session, monkeypatch):
    article = make_article(session)
    monkeypatch.setattr(pagemeta, "fetch", lambda url, timeout=10: PAGE)
    pagemeta.enrich(article)
    assert pagemeta.hashtags(article) == ["BauskasIela", "GāzesSprādziens"]
    assert pagemeta.hashtags(article, limit=1) == ["BauskasIela"]


def test_hashtags_skip_sentence_length_tags(session):
    article = make_article(session)
    article.raw_json = {"_page_meta": {"tags": [
        "Rīga", "kas notiek Bauskas ielā šodien", "A"]}}
    assert pagemeta.hashtags(article) == ["Rīga"]


def test_video_hint_separates_real_clip_from_cms_flag(session):
    article = make_article(session)
    assert pagemeta.video_hint(article) == "nav"

    article.raw_json = {"_page_meta": {"post_types": ["video"]}}
    assert "slideshow" in pagemeta.video_hint(article)

    article.raw_json = {"_page_meta": {"post_types": ["video"]},
                        "_video_url": "https://tv3.lv/v/klips.mp4"}
    assert "īstā klipa" in pagemeta.video_hint(article)


def test_prompt_lines_reads_as_briefing(session, monkeypatch):
    article = make_article(session)
    monkeypatch.setattr(pagemeta, "fetch", lambda url, timeout=10: PAGE)
    pagemeta.enrich(article)
    lines = pagemeta.prompt_lines(article)
    assert "Autors: Gundega Gaujere" in lines
    assert "galerija" in lines
    assert "gars lasāmgabals" in lines
    assert "ekskluzīvs saturs" in lines
    assert pagemeta.is_exclusive(article)
    # video ir atsevišķā rindā (video_hint), lai nesolītu klipu, kura nav
    assert "video der reel" not in lines


def test_prompt_lines_empty_without_metadata(session):
    assert pagemeta.prompt_lines(make_article(session)) == ""


def test_backfill_enriches_older_articles(session, monkeypatch):
    for i in range(3):
        make_article(session, guid=f"g{i}")
    session.commit()
    monkeypatch.setattr(pagemeta, "fetch", lambda url, timeout=10: PAGE)

    assert pagemeta.backfill(session, limit=2) == 2
    assert pagemeta.backfill(session, limit=2) == 1   # only the untouched one is left
    assert pagemeta.backfill(session, limit=2) == 0


def test_articles_page_shows_cms_column(session, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    article = make_article(session)
    monkeypatch.setattr(pagemeta, "fetch", lambda url, timeout=10: PAGE)
    pagemeta.enrich(article)
    session.commit()

    with TestClient(app) as client:
        client.post("/setup", data={"password": "slepens123",
                                    "password2": "slepens123"})
        body = client.get("/articles").text
    assert "Gundega Gaujere" in body
    assert "tv3.lv/p/3879950" in body
    assert "Tikai tv3.lv" in body


def test_preview_shows_author_and_cms_short_link(session, monkeypatch):
    from datetime import timedelta

    from fastapi.testclient import TestClient

    from app.main import app
    from app.models import Post, utcnow

    article = make_article(session)
    monkeypatch.setattr(pagemeta, "fetch", lambda url, timeout=10: PAGE)
    pagemeta.enrich(article)
    post = Post(article_id=article.id, channel="fb_tv3lv", format="link",
                copy="Teksts", link_url=URL, state="scheduled",
                scheduled_at=utcnow() + timedelta(hours=1))
    session.add(post)
    session.commit()

    with TestClient(app) as client:
        client.post("/setup", data={"password": "slepens123",
                                    "password2": "slepens123"})
        body = client.get(f"/post/{post.id}/preview").text
    assert "Gundega Gaujere" in body
    assert "https://tv3.lv/p/3879950" in body


def test_decision_cycle_enriches_and_uses_editorial_tags(session, monkeypatch):
    from sqlalchemy import select

    from app import pipeline
    from app.models import Post

    article = make_article(session, editor_status="must")
    session.commit()
    monkeypatch.setattr(pagemeta, "fetch", lambda url, timeout=10: PAGE)
    monkeypatch.setattr(pipeline, "decide", lambda a, verdicts, s: {
        "publish": True, "score": 0.8, "reason": "t", "labels": [],
        "sensitivity": [],
        "channels": [{"channel": "x_tv3zinas", "format": "text_only",
                      "copy": "Teksts"}],   # AI hashtagus nedod
    })
    pipeline.run_decisions(session)

    assert pagemeta.author(article) == "Gundega Gaujere"   # lapa ievilkta pa ceļam
    post = session.execute(
        select(Post).where(Post.article_id == article.id)).scalars().first()
    assert post is not None
    # X atļauj divus — abi nāk no redakcijas tagiem
    assert post.hashtags == ["#BauskasIela", "#GāzesSprādziens"]


def test_prompt_carries_cms_metadata(session, monkeypatch):
    from app.decide import build_user_prompt
    from app.rules_engine import Verdict

    article = make_article(session, editor_status="must")
    monkeypatch.setattr(pagemeta, "fetch", lambda url, timeout=10: PAGE)
    pagemeta.enrich(article)
    verdicts = {"fb_tv3lv": Verdict(outcome="eligible", reason="ok")}
    prompt = build_user_prompt(article, verdicts, {"fb_tv3lv": {}}, session)

    assert "Autors: Gundega Gaujere" in prompt
    assert "Bauskas iela" in prompt
    assert "slideshow" in prompt          # video ir, klipa saites nav


# --- raksta teksts ierunai -------------------------------------------------

def test_body_text_keeps_the_article_and_drops_the_furniture():
    body = pagemeta.body_text(PAGE)
    assert body.startswith("2026. gada sākumā Rīgu satricināja traģēdija")
    assert "Rīgas pašvaldība apstiprina" in body        # ceturtā rindkopa
    assert "Foto: LETA" not in body                     # attēla paraksts
    assert "Lasi arī" not in body                       # saistītie raksti
    assert "Visas tiesības" not in body                 # kājene
    assert "Ziņas\nSports" not in body                  # izvēlne


def test_body_text_prefers_json_ld():
    html = ('<script type="application/ld+json">'
            '{"@type":"NewsArticle","articleBody":"Pirmā rindkopa ar faktiem.\\n'
            'Otrā rindkopa ar vēl faktiem."}</script>'
            "<p>Šī rindkopa nāk no lapas un tai nevajadzētu uzvarēt pār JSON-LD.</p>")
    assert pagemeta.body_text(html).splitlines() == [
        "Pirmā rindkopa ar faktiem.", "Otrā rindkopa ar vēl faktiem."]


def test_body_text_cuts_at_a_whole_paragraph():
    paras = "".join(f"<p>{'vārds ' * 20}rindkopa numur {i}.</p>" for i in range(20))
    body = pagemeta.body_text(f"<article>{paras}</article>", limit=400)
    assert len(body) <= 400
    assert body.endswith(".")            # nevis pusvārdā
    assert "\n" in body                  # vairāk nekā viena rindkopa


def test_body_text_empty_when_there_is_no_article():
    assert pagemeta.body_text("<html><body><p>Īss.</p></body></html>") == ""
    assert pagemeta.body_text("") == ""


def test_enrich_stores_the_body(session, monkeypatch):
    article = make_article(session)
    monkeypatch.setattr(pagemeta, "fetch", lambda url, timeout=10: PAGE)
    pagemeta.enrich(article)
    assert pagemeta.has_body(article)
    assert "Bauskas ielā 15" in pagemeta.article_body(article)


def test_prompt_carries_the_article_body(session, monkeypatch):
    from app.decide import build_user_prompt
    from app.rules_engine import Verdict

    article = make_article(session, editor_status="must")
    monkeypatch.setattr(pagemeta, "fetch", lambda url, timeout=10: PAGE)
    pagemeta.enrich(article)
    prompt = build_user_prompt(article, {"fb_tv3lv": Verdict("eligible", "ok")},
                               {"fb_tv3lv": {}}, session)
    assert "Raksta teksts (sākums):" in prompt
    assert "daudzdzīvokļu namam tika nodarīti smagi bojājumi" in prompt
    assert "voice_script" in prompt   # reelam ir ko ierunāt


# --- īstā tv3.lv lapas struktūra -------------------------------------------

REAL = (FIXTURES / "article_page_real.html").read_text(encoding="utf-8")


def test_body_comes_from_the_article_container_only():
    """<section class="tv3-single-content"> ir raksta teksts. Bez tā
    atkāpšanās uz visiem lapas <p> ievelka sānjoslu, un AI kartītēs būtu
    rakstījusi par pavisam citu rakstu."""
    paras = pagemeta.paragraphs(REAL)
    assert any("Plkst. 3.11 Igaunijas" in p for p in paras)
    assert any("Uku Arolds" in p for p in paras)
    # sānjosla «Tevi varētu interesēt» un izvēlne paliek ārā
    assert not any("pavisam citu tēmu" in p for p in paras)
    assert not any(p.strip() in ("ZIŅAS", "SPORTS") for p in paras)
    assert not any("Visas tiesības" in p for p in paras)
    assert not any("ALEKSANDR GUSEV" in p for p in paras)


def test_lead_is_kept_even_though_it_sits_outside_the_container():
    paras = pagemeta.paragraphs(REAL)
    assert paras[0].startswith("Droni, kas otrdienas rītā")


def test_editorial_tags_and_sections_from_meta_tags():
    m = pagemeta.parse(REAL)
    assert m["tags"] == ["Droni", "Gaisa apdraudējums", "Igaunija"]
    assert m["categories"] == ["Ziņas", "Ārvalstīs", "Latvijā"]
    assert m["display_category"] == "Ārvalstīs"
    assert m["post_id"] == "3883754"
    assert m["author"] == "LETA"


def test_meta_tags_work_without_any_datalayer():
    """dataLayer var mainīties; og:/article:/cXense tagi ir noturīgāki."""
    html = REAL.split("<script>")[0] + "</head><body></body></html>"
    m = pagemeta.parse(html)
    assert m["post_id"] == "3883754"
    assert m["tags"] == ["Droni", "Gaisa apdraudējums", "Igaunija"]
    assert m["categories"] == ["Ziņas", "Ārvalstīs", "Latvijā"]
    assert m["front_page"] is True


def test_clean_image_skips_the_photopost_graphic():
    """og:image ir photopost ar iecepto virsrakstu; dr:say:img ir īstais foto."""
    m = pagemeta.parse(REAL)
    assert "photopost" not in m["clean_image"]
    assert m["clean_image"].endswith("a6cc-69cac5f108e1e-scaled.jpg")


def test_front_page_position_is_read(session, monkeypatch):
    article = make_article(session, guid="fp-1")
    monkeypatch.setattr(pagemeta, "fetch", lambda url, timeout=10: REAL)
    pagemeta.enrich(article)
    assert pagemeta.front_page(article) == (True, 0)
    lines = pagemeta.prompt_lines(article)
    assert "GALVENAIS stāsts" in lines
    assert "Portālā rādās sadaļā: Ārvalstīs" in lines


def test_cover_falls_back_to_the_pages_clean_image(session, monkeypatch):
    """Plūsmā tikai photopost — vāks tomēr dabū īsto foto no lapas."""
    from app import pipeline

    article = make_article(session, guid="ci-1")
    article.images = ["https://tv3cdn.lv/photopost/3883754/3788760.jpg"]
    monkeypatch.setattr(pagemeta, "fetch", lambda url, timeout=10: REAL)
    pagemeta.enrich(article)

    assert "photopost" not in pipeline.unbranded_image(article)
    clean, blur = pipeline.section_backgrounds(article)
    assert clean and "photopost" not in clean[0]


# --- pilnā lapa: teksts vairākās sadaļās, pilnizmēra attēls ----------------

FULL = (FIXTURES / "article_page_full.html").read_text(encoding="utf-8")


def test_body_spans_every_content_section():
    """tv3.lv sadala rakstu vairākās sadaļās (starp tām reklāma un ieteiktie
    raksti). Ņemot tikai pirmo, no seši rindkopu raksta palika viena."""
    paras = pagemeta.paragraphs(FULL)
    assert len(paras) == 8
    # pirmā sadaļa
    assert any("Plkst. 3.11 Igaunijas" in p for p in paras)
    # otrā sadaļa, PĒC reklāmas un ieteikto rakstu logrīka
    assert any("Uku Arolds" in p for p in paras)
    assert any("Plkst. 4.17 Alūksnes" in p for p in paras)
    assert len(pagemeta.body_text(FULL)) > 1300


def test_embedded_widgets_and_ads_stay_out():
    """«Tevi varētu interesēt» sēž raksta sadaļas IEKŠPUSĒ — tas ir cits
    raksts, un kartītē tas būtu nepareizs fakts."""
    body = pagemeta.body_text(FULL)
    assert "Ogres slimnīcā" not in body          # ieteiktais raksts
    assert "horoskops" not in body               # sānjosla
    assert "Saturs turpinās" not in body         # reklāmas starplika
    assert "Ilustratīvs foto" not in body        # attēla paraksts
    assert "Uzzini plašāk" not in body           # saistīto sadaļa
    assert "Visas tiesības" not in body          # kājene


def test_full_size_image_beats_the_thumbnail():
    """og:image ir photopost, dr:say:img ir 672 px sīktēls — 1080 px kartītē
    tas ir izplūdis. JSON-LD dod oriģinālu."""
    m = pagemeta.parse(FULL)
    assert m["clean_image"] == "https://tv3cdn.lv/2026/03/a6cc-69cac5f108e1e-scaled.jpg"
    assert "thumbnails" not in m["clean_image"]
    assert "photopost" not in m["clean_image"]


def test_widest_srcset_variant_when_no_json_ld():
    """Bez JSON-LD ņemam platāko srcset variantu, nevis to, kas meta tagā."""
    no_ld = FULL.replace("application/ld+json", "application/x-disabled")
    m = pagemeta.parse(no_ld)
    assert "2600x1660" in m["clean_image"]


def test_article_id_datalayer_key():
    """Šī lapa dataLayer'ā lieto «Article ID», nevis «Post ID»."""
    assert pagemeta.parse(FULL)["post_id"] == "3883754"


def test_editor_name_preferred_over_agency():
    """dataLayer šķir žurnālistu (Editor name) no aģentūras (Source)."""
    assert pagemeta.parse(FULL)["author"] == "Justīne Jurcika"


def test_word_count_from_json_ld():
    m = pagemeta.parse(FULL)
    assert m["word_count"] == 1313
