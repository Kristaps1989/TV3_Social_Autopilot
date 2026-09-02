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


def test_mix_deficit_picks_the_starved_format():
    from app.formats import mix_deficit

    # link ir 0% no loga, grīda 0.4 -> tas ir badā
    assert mix_deficit({"photo": 1.0}, {"link": 0.4}, ["link", "photo"]) == "link"
    # kad grīda ir sasniegta, izvēli atkal izlemj svari
    assert mix_deficit({"link": 0.5, "photo": 0.5}, {"link": 0.4},
                       ["link", "photo"]) is None
    # formāts, kas šim rakstam neder, netiek piespiests
    assert mix_deficit({"photo": 1.0}, {"link": 0.4}, ["photo"]) is None
    # bez konfigurētas grīdas nekas nemainās
    assert mix_deficit({"photo": 1.0}, {}, ["link", "photo"]) is None


def test_photo_saturated_feed_gets_a_link_post(session):
    """Regresija: photo uzvarēja katrā neizšķirtā un aizņēma visu plūsmu."""
    from app.formats import choose_format
    from app.models import Article, Post

    a = Article(guid="mix-1", url="https://tv3.lv/a", canonical_url="https://tv3.lv/a",
                title="Parasta ziņa", section="news", images=["https://cdn/i.jpg"])
    session.add(a)
    session.flush()
    for fmt in ("photo", "photo", "photo", "link", "link", "link"):
        session.add(Post(article_id=a.id, channel="fb_mix", format=fmt,
                         state="published"))
    session.commit()
    # photo ir smagāks un to grib arī AI -> bez grīdas tas uzvar
    cfg = {"formats": ["link", "photo"], "format_weights": {"link": 1.0, "photo": 1.5}}
    assert choose_format(session, "fb_mix", cfg, a, ai_choice="photo") == "photo"
    # grīda 0.7 (link šobrīd 0.5) atgriež link postu plūsmā
    cfg_mix = dict(cfg, format_mix={"link": 0.7})
    assert choose_format(session, "fb_mix", cfg_mix, a, ai_choice="photo") == "link"


def test_a_broken_card_overrides_the_link_quota_but_a_survivable_one_does_not(
        session, monkeypatch):
    """Saites grīda tur plūsmā STRĀDĀJOŠUS saites ierakstus.

    Vertikāls attēls tādu nedod: kartīte nogriež 58% augstuma un kopā ar to
    iestrādāto titula plāksni. Piespiest ierakstu palikt saitē tikai tāpēc, ka
    kvota nav pilna, nozīmē uztaisīt sliktu ierakstu, kas kvotu tik un tā
    nepilda — un vēl iemācīt svariem, ka saites posti nestrādā. Mērenam
    apgriezumam grīda paliek svarīgāka; tieši šī ķēde reiz deva 99% photo.
    """
    from app import config, imageinfo, pipeline
    from app.models import Article, Post

    # slieksni lasa no noteikumiem; rediģējamā kopija var būt vecāka par kodu
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    monkeypatch.setattr(imageinfo, "orientation", lambda article: "portrait")
    a = Article(guid="mix-2", url="https://tv3.lv/b", canonical_url="https://tv3.lv/b",
                title="Ziņa ar photopost grafiku", section="news",
                images=["https://cdn/photopost/x.jpg"])
    session.add(a)
    session.flush()
    for _ in range(6):
        session.add(Post(article_id=a.id, channel="fb_mix2", format="photo",
                         state="published"))
    session.commit()
    cfg = {"formats": ["link", "photo"], "platform": "facebook_page",
           "format_mix": {"link": 0.4}}
    # Vertikāls attēls tagad grīdu PĀRSNIEDZ: pie 58% nogriezta augstuma
    # kartīte ir sabojāta neatkarīgi no kvotas, un piespiest to tur nozīmētu
    # uztaisīt sliktu ierakstu, kas kvotu tik un tā nepilda.
    monkeypatch.setattr(imageinfo, "image_size", lambda art, url: (800, 1000))
    fmt, _media, _r = pipeline.resolve_format(session, "fb_mix2", cfg, a, {})
    assert fmt == "photo"

    # Mērens apgriezums (3:2, 21%) grīdu neapiet — tur saites kartīte vēl
    # strādā, un kvota ir svarīgāka
    monkeypatch.setattr(imageinfo, "orientation", lambda art: "landscape")
    monkeypatch.setattr(imageinfo, "image_size", lambda art, url: (1500, 1000))
    fmt, _media, _r = pipeline.resolve_format(session, "fb_mix2", cfg, a, {})
    assert fmt == "link"

    # kad link kvota ir izpildīta, arī mērenais apgriezums pārslēdzas —
    # «mērens» tagad ir virs 30 % (5:4 -> 35 %); 3:2 (21 %) ir FB norma
    session.query(Post).delete()
    for fmt_name in ("link", "link", "link", "photo", "photo", "photo"):
        session.add(Post(article_id=a.id, channel="fb_mix2", format=fmt_name,
                         state="published"))
    session.commit()
    monkeypatch.setattr(imageinfo, "image_size", lambda art, url: (1250, 1000))
    fmt, _media, _r = pipeline.resolve_format(session, "fb_mix2", cfg, a, {})
    assert fmt == "photo"
    # 3:2 pie izpildītas kvotas paliek saite: 21 % nav sabojāta kartīte
    monkeypatch.setattr(imageinfo, "image_size", lambda art, url: (1500, 1000))
    fmt, _media, _r = pipeline.resolve_format(session, "fb_mix2", cfg, a,
                                              {})
    assert fmt == "link"


def test_a_link_card_that_would_cut_heads_becomes_a_photo_post(session, monkeypatch):
    """Saites ierakstā attēlu izvēlas Facebook, un tā 1.91:1 kartīte 4:3 foto
    nogriež 30% augstuma — ziņu kadrā tieši to daļu, kur ir galvas. Photo
    ierakstā attēlu zīmējam mēs, un mūsu rāmji griež sānus, ne augšu."""
    from app import imageinfo, pipeline
    from app.models import Article

    a = Article(guid="crop-1", url="https://tv3.lv/c", canonical_url="https://tv3.lv/c",
                title="Kulbergs kritizē plānu", section="news",
                images=["https://cdn/foto.jpg"], raw_json={})
    session.add(a)
    session.flush()
    cfg = {"formats": ["link", "photo"], "platform": "facebook_page"}
    monkeypatch.setattr(imageinfo, "orientation", lambda art: "landscape")

    # 4:3 -> 30% nost: pārslēdzam
    monkeypatch.setattr(imageinfo, "image_size", lambda art, url: (1200, 900))
    fmt, _m, _r = pipeline.resolve_format(session, "fb_crop", cfg, a, {})
    assert fmt == "photo"

    # 16:9 -> 7% nost: saites kartīte ir labākais formāts, atstājam
    monkeypatch.setattr(imageinfo, "image_size", lambda art, url: (1920, 1080))
    fmt, _m, _r = pipeline.resolve_format(session, "fb_crop", cfg, a, {})
    assert fmt == "link"

    # izmērs nezināms: neziņa nav iemesls atteikties no saites kartītes
    monkeypatch.setattr(imageinfo, "image_size", lambda art, url: None)
    fmt, _m, _r = pipeline.resolve_format(session, "fb_crop", cfg, a, {})
    assert fmt == "link"


def test_the_crop_threshold_is_editable(session, monkeypatch):
    from app import config, imageinfo, pipeline
    from app.models import Article

    a = Article(guid="crop-2", url="https://tv3.lv/d", canonical_url="https://tv3.lv/d",
                title="Ziņa", section="news", images=["https://cdn/f.jpg"],
                raw_json={})
    session.add(a)
    session.flush()
    cfg = {"formats": ["link", "photo"], "platform": "facebook_page"}
    monkeypatch.setattr(imageinfo, "orientation", lambda art: "landscape")
    monkeypatch.setattr(imageinfo, "image_size", lambda art, url: (1500, 1000))

    base = dict(config.load_rules())
    # 3:2 zaudē 21.5%: pie 0.20 pārslēdzam, pie 0.30 vairs ne
    monkeypatch.setattr(config, "load_rules",
                        lambda: {**base, "link_card_max_crop": 0.20})
    assert pipeline.resolve_format(session, "fb_crop2", cfg, a, {})[0] == "photo"
    monkeypatch.setattr(config, "load_rules",
                        lambda: {**base, "link_card_max_crop": 0.30})
    assert pipeline.resolve_format(session, "fb_crop2", cfg, a, {})[0] == "link"
    # 1.0 = nekad
    monkeypatch.setattr(config, "load_rules",
                        lambda: {**base, "link_card_max_crop": 1.0})
    assert pipeline.resolve_format(session, "fb_crop2", cfg, a, {})[0] == "link"


def test_a_queued_link_post_is_retargeted_before_it_goes_out(session, monkeypatch):
    """Ieraksts rindā var nostāvēt stundas. Bez šī labojums aizsniegtu tikai
    tos rakstus, par kuriem lēmums pieņemts PĒC izvietošanas."""
    from app import imageinfo, pipeline
    from app.models import Article, Post

    a = Article(guid="rt-1", url="https://tv3.lv/r", canonical_url="https://tv3.lv/r",
                title="Nepāla publicējusi sarakstu", section="news",
                images=["https://cdn/vertikals.jpg"], raw_json={})
    session.add(a)
    session.flush()
    post = Post(article_id=a.id, channel="fb_rt", format="link", copy="c",
                hashtags=[], link_url="https://tv3.lv/r", state="scheduled")
    session.add(post)
    session.flush()
    cfg = {"formats": ["link", "photo"], "platform": "facebook_page"}
    monkeypatch.setattr(imageinfo, "orientation", lambda art: "portrait")
    monkeypatch.setattr(imageinfo, "image_size", lambda art, url: (800, 1000))

    assert pipeline.retarget_queued_link_post(session, post, cfg) is True
    assert post.format == "photo"
    # redaktors vēlāk var redzēt, KĀPĒC formāts mainījās
    assert post.extra["retargeted"]["from"] == "link"
    assert post.extra["retargeted"]["link_card_crop"] > 0.5

    # otrreiz nav ko darīt
    assert pipeline.retarget_queued_link_post(session, post, cfg) is False


def test_a_queued_link_post_survives_when_the_card_is_fine(session, monkeypatch):
    from app import imageinfo, pipeline
    from app.models import Article, Post

    a = Article(guid="rt-2", url="https://tv3.lv/s", canonical_url="https://tv3.lv/s",
                title="Ziņa", section="news", images=["https://cdn/plats.jpg"],
                raw_json={})
    session.add(a)
    session.flush()
    post = Post(article_id=a.id, channel="fb_rt2", format="link", copy="c",
                hashtags=[], link_url="https://tv3.lv/s", state="scheduled")
    session.add(post)
    session.flush()
    cfg = {"formats": ["link", "photo"], "platform": "facebook_page"}
    monkeypatch.setattr(imageinfo, "orientation", lambda art: "landscape")
    monkeypatch.setattr(imageinfo, "image_size", lambda art, url: (1920, 1080))

    assert pipeline.retarget_queued_link_post(session, post, cfg) is False
    assert post.format == "link"
