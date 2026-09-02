"""Facebook prakse pārnesta uz Instagram, Threads un X: saites vieta pa
platformām, norāde apraksta beigās, vienreiz uzbūvētas lentes/karuseļa
koplietošana starp kanāliem, konfigurācijas dreifa redzamība."""
from app import config
from app.models import Article, Post
from app.pipeline import compose_text, link_placement, link_pointer, resolve_format


def _post(session, platform_channel="ig", fmt="photo", sensitivity=None):
    a = Article(guid=f"ps-{platform_channel}-{fmt}", url="https://tv3.lv/ps",
                canonical_url="https://tv3.lv/ps", title="Ziņa", section="news",
                sensitivity=sensitivity or [], raw_json={},
                images=["https://tv3.lv/i.jpg"])
    session.add(a)
    session.flush()
    p = Post(article_id=a.id, channel=platform_channel, format=fmt,
             copy="Kas notika Bauskas ielā", hashtags=["#tv3"],
             link_url=a.canonical_url, state="scheduled")
    session.add(p)
    session.flush()
    return p


BASE = dict(config.load_rules(), link_in_first_comment=True, link_in_caption=True)


def test_link_placement_per_platform():
    # saites ieraksts: saite vienmēr tekstā, nekad komentārā
    assert link_placement("x", "link", BASE) == (True, False)
    assert link_placement("instagram", "text_only", BASE) == (True, False)
    # FB medijs: aprakstā UN pirmajā komentārā
    assert link_placement("facebook_page", "photo", BASE) == (True, True)
    assert link_placement("facebook_page", "reel",
                          dict(BASE, link_in_caption=False)) == (False, True)
    # X / Threads: noklusēti tekstā; ar atslēgu — atbildē
    assert link_placement("x", "reel", BASE) == (True, False)
    assert link_placement("x", "reel", dict(BASE, x_link_in_reply=True)) == (False, True)
    assert link_placement("threads", "card_carousel",
                          dict(BASE, threads_link_in_reply=True)) == (False, True)


def test_instagram_caption_has_no_link_but_a_pointer_and_a_comment(session):
    p = _post(session, "ig", "reel")
    text, in_comment = compose_text(p, "instagram", "https://tv3.lv/ps?utm_content=1",
                                    rules=BASE)
    assert in_comment
    assert "tv3.lv/ps" not in text          # IG apraksta saite nav klikšķināma
    assert "Saite komentāros 👇" in text
    assert text.rstrip().endswith("👇") or "#tv3" in text


def test_pointer_is_not_doubled_when_copy_already_says_it(session):
    p = _post(session, "ig2", "photo")
    p.copy = "Viss stāsts — saite komentāros."
    text, _ = compose_text(p, "instagram", "https://tv3.lv/ps", rules=BASE)
    assert text.count("komentār") == 1


def test_sober_topics_get_the_pointer_without_emoji(session):
    p = _post(session, "ig3", "photo", sensitivity=["tragedy"])
    assert link_pointer("instagram", p, BASE) == "Saite komentāros"
    text, _ = compose_text(p, "instagram", "https://tv3.lv/ps", rules=BASE)
    assert "Saite komentāros" in text and "👇" not in text


def test_x_link_stays_in_text_by_default_and_moves_to_reply_on_request(session):
    p = _post(session, "x", "reel")
    text, in_comment = compose_text(p, "x", "https://tv3.lv/ps?u=1", rules=BASE)
    assert "https://tv3.lv/ps?u=1" in text and not in_comment
    assert "atbildē" not in text

    text, in_comment = compose_text(p, "x", "https://tv3.lv/ps?u=1",
                                    rules=dict(BASE, x_link_in_reply=True))
    assert in_comment and "tv3.lv/ps" not in text
    assert "Saite atbildē 👇" in text


def test_threads_pointer_is_configurable(session):
    p = _post(session, "th", "photo")
    rules = dict(BASE, threads_link_in_reply=True, reply_link_pointer="Raksts atbildē")
    text, in_comment = compose_text(p, "threads", "https://tv3.lv/ps", rules=rules)
    assert in_comment and "tv3.lv/ps" not in text
    assert text.split("\n\n")[1] == "Raksts atbildē"   # pirms hashtag rindas


def test_facebook_caption_needs_no_pointer(session):
    p = _post(session, "fb", "photo")
    text, in_comment = compose_text(p, "facebook_page", "https://tv3.lv/ps", rules=BASE)
    assert in_comment and "tv3.lv/ps" in text and "komentār" not in text.lower()


def _article_with_built(session, fmt, media, recipe=None, channel="fb"):
    a = Article(guid=f"bm-{fmt}-{channel}", url="https://tv3.lv/bm",
                canonical_url="https://tv3.lv/bm", title="T", section="news",
                images=["https://tv3.lv/i.jpg"], raw_json={})
    session.add(a)
    session.flush()
    session.add(Post(article_id=a.id, channel=channel, format=fmt, media=media,
                     copy="c", state="scheduled",
                     extra={"recipe": recipe or {"voiced": True, "k": 1}}))
    session.flush()
    return a


def test_second_channel_reuses_the_reel_built_for_the_first(session, monkeypatch, tmp_path):
    from app import pipeline

    clip = tmp_path / "r.mp4"
    clip.write_bytes(b"mp4")
    a = _article_with_built(session, "reel", [str(clip)])
    from app import reels

    def rebuilt(*a, **k):
        raise AssertionError("lente uzbūvēta otrreiz")

    monkeypatch.setattr(reels, "build_reel", rebuilt)
    monkeypatch.setattr(reels, "build_video_reel", rebuilt)
    monkeypatch.setattr(reels, "available", lambda: True)
    cfg = {"formats": ["link", "reel"], "platform": "x"}
    fmt, media, recipe = resolve_format(session, "x", cfg, a,
                                        {"format": "reel", "card_sections": []})
    assert fmt == "reel"
    assert media == [str(clip)]
    assert recipe == {"voiced": True, "k": 1}


def test_reuse_skips_posts_whose_files_are_gone_and_respects_the_rule(session, monkeypatch, tmp_path):
    from app import pipeline

    a = _article_with_built(session, "card_carousel", [str(tmp_path / "gone.png")],
                            channel="fb-gone")
    assert pipeline.built_media(session, a, "card_carousel") is None

    good = tmp_path / "c1.png"
    good.write_bytes(b"png")
    session.add(Post(article_id=a.id, channel="fb", format="card_carousel",
                     media=[str(good)], copy="c", state="published",
                     extra={"recipe": {"sections": 3}}))
    session.flush()
    assert pipeline.built_media(session, a, "card_carousel") == ([str(good)], {"sections": 3})
    assert pipeline.built_media(session, a, "card_carousel",
                                rules={"share_built_media": False}) is None
    # citam formātam koplietošanas nav
    assert pipeline.built_media(session, a, "photo") is None


def test_reuse_only_when_the_channel_accepts_the_format(session, monkeypatch, tmp_path):
    from app import pipeline

    clip = tmp_path / "r.mp4"
    clip.write_bytes(b"mp4")
    a = _article_with_built(session, "reel", [str(clip)], channel="fb-r")
    cfg = {"formats": ["link", "photo"], "platform": "threads"}
    fmt, media, _ = resolve_format(session, "th", cfg, a, {"format": "reel"})
    assert fmt != "reel"
    assert media != [str(clip)]


def test_missing_channel_formats_reports_drift(tmp_path, monkeypatch):
    default, editable = tmp_path / "repo", tmp_path / "data"
    default.mkdir(), editable.mkdir()
    (default / "channels.yaml").write_text(
        "x:\n  platform: x\n  feeds: [news_all]\n  formats: [link, photo, reel, card_carousel]\n"
        "th:\n  platform: threads\n  feeds: [news_all]\n  formats: [link]\n"
        "new_ch:\n  platform: x\n  feeds: [news_all]\n  formats: [link]\n")
    (editable / "channels.yaml").write_text(
        "x:\n  platform: x\n  feeds: [news_all]\n  formats: [link, photo]\n"
        "th:\n  platform: threads\n  feeds: [news_all]\n  formats: [link, photo]\n")
    monkeypatch.setattr(config, "RULES_DIR", editable)
    monkeypatch.setattr(config, "DEFAULT_RULES_DIR", default)
    # trūkstošie formāti tikai esošajiem kanāliem; jauns kanāls ir missing_channels lieta
    assert config.missing_channel_formats() == {"x": ["reel", "card_carousel"]}


def test_missing_channel_formats_is_quiet_without_an_editable_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RULES_DIR", tmp_path / "nope")
    assert config.missing_channel_formats() == {}


def test_every_platform_has_a_style_prompt(monkeypatch):
    # koda prompti, ne šīs mašīnas rediģējamā kopija
    monkeypatch.setattr(config, "PROMPTS_DIR", config.DEFAULT_PROMPTS_DIR)
    for platform in ("facebook_page", "instagram", "threads", "x"):
        assert config.system_prompt_for(platform).strip(), platform
    assert "komentār" in config.system_prompt_for("instagram").lower()
    assert "reel" in config.system_prompt_for("x").lower()


def test_platform_specs_list_the_formats_adapters_support():
    from app.best_practices import PLATFORM_SPECS

    assert "reel" in PLATFORM_SPECS["x"].formats
    assert "card_carousel" in PLATFORM_SPECS["threads"].formats
    assert "reel" in PLATFORM_SPECS["instagram"].formats
    assert not PLATFORM_SPECS["instagram"].link_in_copy


def test_settings_page_shows_instagram_prompt_and_reset_restores_the_code_default(
        session, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app import main as main_mod
    from app.main import app

    prompts = tmp_path / "prompts"
    prompts.mkdir()
    monkeypatch.setattr(config, "PROMPTS_DIR", prompts)
    monkeypatch.setitem(main_mod.EDITABLE, "prompt_instagram", prompts / "system_instagram.md")
    monkeypatch.setitem(main_mod.EDITABLE, "prompt_x", prompts / "system_x.md")
    (prompts / "system_x.md").write_text("vecs X prompts\n", encoding="utf-8")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    with TestClient(app) as client:
        client.post("/setup", data={"password": "slepens123", "password2": "slepens123"})
        r = client.get("/settings")
        assert r.status_code == 200
        assert "AI stils — Instagram" in r.text
        assert "Atjaunot no koda" in r.text          # X kopija atšķiras no koda
        r = client.post("/settings/prompt_x/reset", follow_redirects=False)
        assert r.status_code == 303 and "atjaunots" in r.headers["location"]
        assert (prompts / "system_x.md").read_text(encoding="utf-8") == \
            (config.DEFAULT_PROMPTS_DIR / "system_x.md").read_text(encoding="utf-8")
        # noteikumus/kanālus no koda nepārraksta — tur katra rinda ir redakcijas lēmums
        r = client.post("/settings/rules/reset", follow_redirects=False)
        assert r.status_code == 303 and "error" in r.headers["location"]
