"""Karuselis un lente vairs nepārņem plūsmu: dienas kvota, saites grīda,
neveiksmes ar iemeslu un kvotu fakti promptā."""
from datetime import timedelta

from sqlalchemy import select

from app import cards, config, decide, pipeline, reels
from app.models import Article, Evaluation, Post, utcnow

CFG = {"platform": "facebook_page", "formats": ["link", "photo", "card_carousel", "reel"],
       "format_mix": {"link": 0.4}, "format_daily_cap": {"card_carousel": 2, "reel": 1}}
SECTIONS = [{"title": "Kas notika", "body": "Pirmais teksts ir gana garš, lai sadaļa derētu."},
            {"title": "Kas tālāk", "body": "Otrais teksts arī ir gana garš, lai sadaļa derētu."}]


def _article(session, guid="fd-1"):
    a = Article(guid=guid, url=f"https://tv3.lv/{guid}", canonical_url=f"https://tv3.lv/{guid}",
                title="Skaidrojums par vēju", section="news", editor_status="must",
                images=["https://tv3.lv/i.jpg"],
                published_at=utcnow() - timedelta(minutes=5))
    session.add(a)
    session.flush()
    return a


def _post(session, article, fmt, channel="fb_q", when=None, created=None):
    p = Post(article_id=article.id, channel=channel, format=fmt, copy="x",
             state="scheduled", scheduled_at=when or utcnow(),
             created_at=created or utcnow())
    session.add(p)
    session.flush()
    return p


def _history_append(session, article, formats, channel="fb_q"):
    """Vēsture plūsmas galā, neaizskarot vecākos ierakstus."""
    base = utcnow() - timedelta(minutes=len(formats) + 1)
    for i, fmt in enumerate(formats):
        _post(session, article, fmt, channel=channel,
              when=base + timedelta(minutes=i), created=base + timedelta(minutes=i))
    session.commit()


def _history(session, article, formats, channel="fb_q"):
    """Kanāla vēsture, vecākais pirmais — laiki skaidri atšķirīgi, lai
    «pēc kārtas» sargs testā nav atkarīgs no vienlaicīgiem zīmogiem."""
    base = utcnow() - timedelta(days=1)
    for i, fmt in enumerate(formats):
        _post(session, article, fmt, channel=channel, when=base + timedelta(minutes=i),
              created=base + timedelta(minutes=i))
    session.commit()


def _fake_carousel(monkeypatch):
    monkeypatch.setattr(cards, "renderer_available", lambda: True)
    monkeypatch.setattr(cards, "render_section_cards",
                        lambda *a, **k: ["data/cards/c1.png", "data/cards/c2.png"])
    monkeypatch.setattr(pipeline, "section_backgrounds", lambda article: ([], ""))
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)


def test_daily_quota_turns_the_third_carousel_into_a_link(session, monkeypatch):
    _fake_carousel(monkeypatch)
    a = _article(session)
    # kvota brīva un saites grīda izpildīta -> karuselis
    for fmt in ("link", "link", "link", "photo"):
        _post(session, a, fmt, when=utcnow() - timedelta(days=2))
    notes: list[str] = []
    fmt, media, _ = pipeline.resolve_format(session, "fb_q", CFG, a,
                                            {"format": "card_carousel", "card_sections": SECTIONS},
                                            notes=notes)
    assert fmt == "card_carousel" and notes == []

    _post(session, a, "card_carousel")
    _post(session, a, "card_carousel")
    for fmt in ("link", "link", "link"):
        _post(session, a, fmt, when=utcnow() - timedelta(days=2))
    notes = []
    fmt, media, _ = pipeline.resolve_format(session, "fb_q", CFG, a,
                                            {"format": "card_carousel", "card_sections": SECTIONS},
                                            notes=notes)
    assert fmt != "card_carousel"
    assert notes and "kvota 2/2" in notes[0]
    # rokas režīms kvotu neskata
    fmt, _m, _r = pipeline.resolve_format(session, "fb_q", CFG, a,
                                          {"format": "card_carousel", "card_sections": SECTIONS},
                                          enforce=False)
    assert fmt == "card_carousel"


def test_link_floor_beats_an_ai_carousel(session, monkeypatch):
    """Saites grīda pati par sevi: plūsma bez atkārtojuma pēc kārtas (foto un
    karuselis pamīšus), bet nevienas saites — nākamajam jābūt saitei."""
    _fake_carousel(monkeypatch)
    a = _article(session, "fd-2")
    # karuselis 2/6 (zem 35 % griestiem), plūsmas galā nav atkārtojuma,
    # bet nevienas saites — paliek tikai grīdas arguments
    _history(session, a, ["photo", "card_carousel", "photo", "card_carousel",
                          "photo", "photo"])
    notes: list[str] = []
    fmt, _m, _r = pipeline.resolve_format(session, "fb_q", CFG, a,
                                          {"format": "card_carousel", "card_sections": SECTIONS},
                                          notes=notes)
    assert fmt == "link"
    assert any("saites grīda" in n for n in notes), notes


def test_the_same_format_never_runs_three_times_in_a_row(session, monkeypatch):
    """Skaits nav galvenais: pat ar brīvu dienas kvotu trešais vienāds
    ieraksts pēc kārtas ir vienveidība, un formāts konkursā nepiedalās."""
    from app.formats import choose_format

    _fake_carousel(monkeypatch)
    a = _article(session, "fd-run")
    # divi karuseļi pēc kārtas plūsmas galā, kvota (2) vēl neizpildīta vakar
    _history(session, a, ["card_carousel", "card_carousel"])
    notes: list[str] = []
    fmt, _m, _r = pipeline.resolve_format(session, "fb_q", CFG, a,
                                          {"format": "card_carousel", "card_sections": SECTIONS},
                                          notes=notes)
    assert fmt != "card_carousel"
    assert any("pēc kārtas" in n for n in notes), notes

    # tas pats attiecas uz parastajiem formātiem, ne tikai AI izvēli
    session.query(Post).delete()
    session.commit()
    _history(session, a, ["photo", "photo"])
    assert choose_format(session, "fb_q", CFG, a) != "photo"


def test_a_format_over_its_share_ceiling_sits_out(session, monkeypatch):
    """Griesti daļai: bez atkārtojuma pēc kārtas, bet 50 % karuseļu pēdējos
    ierakstos jau ir virs 35 % griestiem."""
    _fake_carousel(monkeypatch)
    a = _article(session, "fd-share")
    _history(session, a, ["card_carousel", "link", "card_carousel", "link",
                          "card_carousel", "link"])
    notes: list[str] = []
    fmt, _m, _r = pipeline.resolve_format(session, "fb_q", CFG, a,
                                          {"format": "card_carousel", "card_sections": SECTIONS},
                                          notes=notes)
    assert fmt != "card_carousel"
    assert any("griesti" in n for n in notes), notes


def test_paid_results_nudge_the_format_only_when_ads_are_live(session, monkeypatch):
    """Reklāmas arguments ir izmērīts, ne pieņemts: boostot var visus trīs
    formātus, tāpēc svaru dod sesijas par eiro — un tikai tad, kad reklāmas
    tiešām iet ārā."""
    from app import ads
    from app.formats import ad_multipliers
    from app.models import AdEntry

    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    a = _article(session, "fd-paid")
    for fmt_name, sessions in (("link", 90), ("link", 80), ("link", 100),
                               ("photo", 20), ("photo", 30), ("photo", 25)):
        p = _post(session, a, fmt_name, channel="fb_paid")
        session.add(AdEntry(post_id=p.id, article_id=a.id, platform="facebook_page",
                            status="done", spent_cents=1000, sessions=sessions))
    session.commit()

    ads.save_settings(session, "dry", 20.0, 0)
    assert ad_multipliers(session, "fb_paid") == {}      # nauda neiet — nav argumenta

    ads.save_settings(session, "auto", 20.0, 0)
    mults = ad_multipliers(session, "fb_paid")
    assert mults["link"] > 1.0 > mults["photo"]
    assert 0.85 <= mults["photo"] and mults["link"] <= 1.2   # koriģē, neizšķir


def test_reel_failure_is_recorded_and_explained(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    monkeypatch.setattr(reels, "available", lambda: True)

    def boom(*a, **kw):
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr(reels, "build_reel", boom)
    monkeypatch.setattr(pipeline, "section_backgrounds", lambda article: ([], ""))
    monkeypatch.setattr(pipeline, "unbranded_image", lambda article: "")
    a = _article(session, "fd-3")
    for fmt in ("link", "link", "link", "photo"):
        _post(session, a, fmt, when=utcnow() - timedelta(days=2))
    notes: list[str] = []
    fmt, _m, _r = pipeline.resolve_format(session, "fb_q", CFG, a,
                                          {"format": "reel", "card_sections": SECTIONS},
                                          notes=notes)
    assert fmt in ("link", "photo")
    assert any("lentes būve neizdevās" in n and "ffmpeg exploded" in n for n in notes)
    assert "reel" in cards.last_render_failure()

    monkeypatch.setattr(reels, "available", lambda: False)
    notes = []
    pipeline.resolve_format(session, "fb_q", CFG, a,
                            {"format": "reel", "card_sections": SECTIONS}, notes=notes)
    assert any("nav pieejams" in n for n in notes)


def test_the_wave_records_why_the_ai_format_was_not_used(session, monkeypatch):
    _fake_carousel(monkeypatch)
    a = _article(session, "fd-4")
    _history(session, a, ["photo", "card_carousel", "photo", "card_carousel",
                          "photo", "photo"], channel="fb_tv3lv")
    decision = {"publish": True, "reason": "", "channels": [
        {"channel": "fb_tv3lv", "format": "card_carousel", "copy": "C", "hook_type": "fact",
         "card_sections": SECTIONS}]}
    monkeypatch.setattr(pipeline, "decide", lambda article, verdicts, session: decision)
    b = Article(guid="fd-5", url="https://tv3.lv/fd5", canonical_url="https://tv3.lv/fd5",
                title="Jauna ziņa", section="news", editor_status="must",
                images=["https://tv3.lv/i.jpg"], published_at=utcnow() - timedelta(minutes=5))
    session.add(b)
    session.commit()
    pipeline.run_decisions(session)
    post = session.execute(select(Post).where(Post.article_id == b.id,
                                              Post.channel == "fb_tv3lv")).scalar_one()
    assert post.format == "link"
    assert any("saites grīda" in n for n in post.extra["format_notes"])
    ev = session.execute(select(Evaluation).where(Evaluation.article_id == b.id,
                                                  Evaluation.outcome == "posted")).scalar_one()
    assert "formāts: card_carousel" in ev.reason


def test_prompt_states_todays_quotas_as_facts(session, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    a = _article(session, "fd-6")
    _post(session, a, "card_carousel", channel="fb_tv3lv")
    _post(session, a, "card_carousel", channel="fb_tv3lv")
    cfg = config.load_channels()
    text = decide.format_quota_context(session, ["fb_tv3lv"], cfg)
    assert "2/8 card_carousel" in text and "0/4 reel" in text
    assert "nepiedāvā: card_carousel (pēdējie 2" in text   # atkārtojums, ne kvota
    assert text.count("(pēdējie") == 1                      # bez dublikātiem
    assert "priekšroka: link (saites grīda" in text        # ko gaidām, atsevišķi
    assert "saites daļa" in text


# --- sargu konflikts, kas deva 8 foto pēc kārtas ---------------------------

def _portrait_og(monkeypatch):
    """tv3.lv raksts, kura og:image ir portreta photopost: saites kartīte
    būtu sabojāta, tāpēc saite līdz šim VIENMĒR kļuva par foto."""
    from app import imageinfo

    monkeypatch.setattr(imageinfo, "orientation", lambda article: "portrait")
    monkeypatch.setattr(imageinfo, "image_size", lambda article, url: (1080, 1350))


def test_a_broken_link_card_does_not_override_the_photo_run_guard(session, monkeypatch):
    """Plūsmas galā jau divi foto: nākamais raksts ar portreta og:image
    tomēr iet kā saite (nogriezta kartīte ir labāka par trešo foto)."""
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    _portrait_og(monkeypatch)
    a = _article(session, "loop-1")
    _history(session, a, ["photo", "photo"])
    fmt, _m, _r = pipeline.resolve_format(session, "fb_q", CFG, a, {"format": "link"})
    assert fmt == "link"
    # bez atkārtojuma plūsmas galā kartītes sargs strādā kā līdz šim
    session.query(Post).delete()
    session.commit()
    _history(session, a, ["link", "photo", "link"])
    fmt, _m, _r = pipeline.resolve_format(session, "fb_q", CFG, a, {"format": "link"})
    assert fmt == "photo"


def test_link_floor_does_not_kill_a_carousel_when_a_link_is_impossible(session, monkeypatch):
    """Saites daļa 0 %, bet šim rakstam saite tik un tā kļūtu par foto —
    tad karuselis ir tieši tā dažādība, ko plūsma prasa."""
    _fake_carousel(monkeypatch)
    _portrait_og(monkeypatch)
    a = _article(session, "loop-2")
    # ekrānuzņēmuma stāvoklis: seši foto pēc kārtas, saišu nav
    _history(session, a, ["photo"] * 6)
    notes: list[str] = []
    fmt, _m, _r = pipeline.resolve_format(session, "fb_q", CFG, a,
                                          {"format": "card_carousel", "card_sections": SECTIONS},
                                          notes=notes)
    assert fmt == "card_carousel", notes


def test_the_feed_rotates_instead_of_collapsing_into_photos(session, monkeypatch):
    """Astoņi raksti pēc kārtas, visiem portreta og:image, AI katram saka
    «link»: rezultāts drīkst būt foto un saites pamīšus, ne astoņi foto."""
    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    _portrait_og(monkeypatch)
    formats = []
    for i in range(8):
        a = _article(session, f"loop-3-{i}")
        fmt, _m, _r = pipeline.resolve_format(session, "fb_q", CFG, a, {"format": "link"})
        _history(session, a, [fmt])
        formats.append(fmt)
    assert formats.count("photo") <= 5 and formats.count("link") >= 3
    # nekad trīs vienādi pēc kārtas
    assert not any(formats[i] == formats[i + 1] == formats[i + 2] for i in range(6))


def test_the_trace_explains_every_format_decision(session, monkeypatch):
    """Diagnostikas pēda (`formats.explain`, scripts/format_report.py): kāpēc
    tieši šis formāts, kurš sargs ko bloķēja un ar kādiem svariem."""
    from app.formats import explain, row_limit

    monkeypatch.setattr(config, "RULES_DIR", config.DEFAULT_RULES_DIR)
    a = _article(session, "tr-1")
    _history(session, a, ["photo", "photo"])
    trace = explain(session, "fb_q", CFG, a, "photo")
    assert trace["chosen"] != "photo"
    assert "pēc kārtas" in trace["blocked"]["photo"]
    assert trace["run"] == {"format": "photo", "count": 2}
    assert trace["shares"]["photo"] == 1.0
    assert trace["ai_choice"] == "photo"
    # saites daļa 0 % zem grīdas -> grīda izšķir, un pēda to pasaka
    assert trace["chosen"] == "link" and "grīda" in trace["decision"]

    # kad grīda ir izpildīta, redzami visi svaru komponenti
    session.query(Post).delete()
    session.commit()
    _history(session, a, ["link", "link", "photo", "link", "photo", "link"])
    scored = explain(session, "fb_q", CFG, a, "photo")
    assert scored["scores"]["photo"]["AI izvēle"] == 1.25
    assert set(scored["scores"]["photo"]) >= {"total", "svars", "izmērītais",
                                              "sadaļa", "reklāma", "piesātinājums"}

    # kad neviens formāts nav tīrs, izvēlas mazāko ļaunumu, nevis atlaiž sargu
    session.query(Post).delete()
    session.commit()
    _history(session, a, ["photo", "photo", "photo", "link", "link", "link"])
    trace = explain(session, "fb_q", CFG, a)
    assert trace["chosen"] == "photo"                    # link = 3 pēc kārtas
    assert "mazāko ļaunumu" in trace["note"]
    assert "pēc kārtas" in trace["blocked"]["link"]

    # 0 = sargs izslēgts (agrāk `or` to klusi pārvērta par noklusējumu)
    assert row_limit({"max_same_format_in_row": 0}) == 0
    assert row_limit({}) == 2
    off = dict(CFG, max_same_format_in_row=0, format_max_share={"photo": 1.1})
    assert explain(session, "fb_q", off, a)["blocked"].get("photo") is None


def test_the_wave_stores_the_trace_on_the_post(session, monkeypatch):
    from sqlalchemy import select as _select

    _fake_carousel(monkeypatch)
    a = _article(session, "tr-2")
    _history(session, a, ["photo", "photo"], channel="fb_tv3lv")
    decision = {"publish": True, "reason": "", "channels": [
        {"channel": "fb_tv3lv", "format": "photo", "copy": "C", "hook_type": "fact"}]}
    monkeypatch.setattr(pipeline, "decide", lambda article, verdicts, session: decision)
    b = Article(guid="tr-3", url="https://tv3.lv/tr3", canonical_url="https://tv3.lv/tr3",
                title="Jauna ziņa", section="news", editor_status="must",
                images=["https://tv3.lv/i.jpg"], published_at=utcnow() - timedelta(minutes=5))
    session.add(b)
    session.commit()
    pipeline.run_decisions(session)
    post = session.execute(_select(Post).where(Post.article_id == b.id,
                                               Post.channel == "fb_tv3lv")).scalar_one()
    trace = post.extra["format_trace"]
    assert trace["chosen"] == post.format
    assert trace["decision"] and trace["run"]["count"] == 2


# --- mērogs un platformu atšķirības (no dzīvās diagnostikas) ---------------

def test_a_single_format_channel_is_never_called_monotonous(session):
    """Stāstu kanālam ir tikai viens formāts: «pēdējie 6 ir story» tur nav
    problēma, un sargs to nedrīkst apturēt (diagnostikā tas izskatījās pēc
    kļūdas, un `rich_format_gate` to būtu aizturējis)."""
    from app.formats import monotony_reason, over_max_share, repeats_too_much

    cfg = {"platform": "facebook_page", "formats": ["story"]}
    a = _article(session, "sf-1")
    _history(session, a, ["story"] * 6, channel="fb_stories")
    assert repeats_too_much(session, "fb_stories", cfg, "story") is False
    assert over_max_share(session, "fb_stories", cfg, "story") is False
    assert monotony_reason(session, "fb_stories", cfg, "story") == ""


def test_photo_ceiling_is_looser_where_photo_is_the_recommended_format(session):
    """X un Threads saites kartīte virsrakstu nerāda, tāpēc brendēts foto tur
    ir ieteicamais formāts — Facebook 50 % griesti tur nozīmētu piespiedu
    teksta ierakstus."""
    from app.formats import max_shares, over_max_share

    a = _article(session, "pl-1")
    _history(session, a, ["photo", "photo", "photo", "photo", "link", "link"],
             channel="x_tv3zinas")          # foto 67 %
    x_cfg = {"platform": "x", "formats": ["link", "photo", "text_only"]}
    fb_cfg = {"platform": "facebook_page", "formats": ["link", "photo"]}
    assert max_shares(x_cfg)["photo"] == 0.7
    assert max_shares(fb_cfg)["photo"] == 0.5
    assert over_max_share(session, "x_tv3zinas", x_cfg, "photo") is False
    assert over_max_share(session, "x_tv3zinas", fb_cfg, "photo") is True
    # kanāla sava vērtība uzvar abus
    own = dict(x_cfg, format_max_share={"photo": 0.5})
    assert over_max_share(session, "x_tv3zinas", own, "photo") is True


def test_the_daily_cap_is_a_backstop_not_the_rotation(session, monkeypatch):
    """Kvota 2 pie ~30 ierakstiem dienā nozīmēja, ka formāts pēc diviem
    ierakstiem pazūd uz visu dienu; rotāciju dara griesti un atkārtojums."""
    _fake_carousel(monkeypatch)
    a = _article(session, "cap-1")
    cfg = dict(CFG, format_daily_cap=None)     # koda noklusējumi
    # četri karuseļi šodien, bet VECĀKI par pēdējiem sešiem: kvotā tie skaitās,
    # plūsmas galā nav (citādi nostrādātu atkārtojuma sargs, ne kvota)
    early = utcnow() - timedelta(hours=6)
    for i in range(4):
        _post(session, a, "card_carousel", when=utcnow(),
              created=early + timedelta(minutes=i))
    session.commit()
    _history_append(session, a, ["link", "photo", "link", "photo", "link", "photo"])
    assert pipeline.format_daily_cap(cfg, "card_carousel") == 8
    assert pipeline.rich_format_gate(session, "fb_q", cfg, a, "card_carousel") == ""
    for i in range(4, 8):
        _post(session, a, "card_carousel", when=utcnow(),
              created=early + timedelta(minutes=i))   # astoņi — drošinātājs
    session.commit()
    assert "kvota 8/8" in pipeline.rich_format_gate(session, "fb_q", cfg, a, "card_carousel")
