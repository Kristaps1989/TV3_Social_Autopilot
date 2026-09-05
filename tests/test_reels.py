import httpx
import pytest

from app.models import Article


def _article(session, guid="r-1"):
    a = Article(guid=guid, url=f"https://tv3.lv/{guid}",
                canonical_url=f"https://tv3.lv/{guid}",
                title="Skaidrojums par notikumu", section="news",
                images=["https://cdn/img.png"], raw_json={})
    session.add(a)
    session.flush()
    return a


def test_resolve_format_builds_reel(session, monkeypatch):
    from app import pipeline, reels

    monkeypatch.setattr(reels, "available", lambda: True)
    built = {}

    def fake_build(title, section, image, points, out_dir=None, voice=None,
                   sections=None, point_images=None, **kw):
        built.update(title=title, points=points, voice=voice,
                     sections=sections, **kw)
        return "/data/cards/reel_x.mp4"

    monkeypatch.setattr(reels, "build_reel", fake_build)
    cfg = {"formats": ["photo", "reel"], "platform": "instagram"}
    fmt, media, _r = pipeline.resolve_format(session, "ig", cfg, _article(session), {
        "format": "reel", "card_points": ["Pirmais āķis", "Otrais āķis"]})
    assert fmt == "reel"
    assert media == ["/data/cards/reel_x.mp4"]
    assert built["points"] == ["Pirmais āķis", "Otrais āķis"]

    # too few points -> falls back to a normal format
    fmt, media, _r = pipeline.resolve_format(session, "ig", cfg, _article(session, "r-2"),
                                         {"format": "reel", "card_points": ["Viens"]})
    assert fmt != "reel"


def test_reel_not_offered_to_chooser():
    from app.formats import suitable_formats

    a = Article(guid="r-3", url="u", canonical_url="u", title="T", section="news",
                images=["https://cdn/i.png"], raw_json={})
    assert "reel" not in suitable_formats(a, ["photo", "reel", "link"])


def test_instagram_reel_flow(monkeypatch):
    from adapters import instagram

    monkeypatch.setattr(instagram.credentials, "get",
                        lambda key, session=None: {"ig_user_id": "178",
                                                   "fb_page_token": "tok"}.get(key, ""))
    calls = []

    class R:
        status_code = 200

        def __init__(self, payload):
            self._p = payload

        def json(self):
            return self._p

    def fake_post(url, data=None, timeout=None):
        calls.append((url, dict(data)))
        return R({"id": f"c{len(calls)}"})

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://app.example")
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get",
                        lambda url, params=None, timeout=None: R({"status_code": "FINISHED"}))
    out = instagram.InstagramAdapter().publish(
        text="Apraksts", link="", images=["/data/cards/reel_x.mp4"], fmt="reel")
    assert out == "c2"
    assert calls[0][1]["media_type"] == "REELS"
    assert calls[0][1]["video_url"].endswith("/media/reel_x.mp4")
    assert calls[1][0].endswith("/media_publish")


def test_facebook_reel_flow(monkeypatch, tmp_path):
    from adapters import facebook

    monkeypatch.setattr(facebook.credentials, "get",
                        lambda key, session=None: {"fb_page_id": "520",
                                                   "fb_page_token": "tok"}.get(key, ""))
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"mp4-bytes")
    graph_calls, uploads = [], []

    class R:
        def __init__(self, status, payload):
            self.status_code = status
            self._p = payload
            self.text = str(payload)

        def json(self):
            return self._p

    def fake_post(url, data=None, files=None, content=None, headers=None, timeout=None):
        if "rupload" in url:
            uploads.append((url, content, dict(headers or {})))
            return R(200, {"success": True})
        graph_calls.append((url, dict(data or {})))
        if data.get("upload_phase") == "start":
            return R(200, {"video_id": "v9",
                           "upload_url": "https://rupload.facebook.com/video-upload/v21.0/v9"})
        return R(200, {"success": True})

    monkeypatch.setattr(httpx, "post", fake_post)
    out = facebook.FacebookPageAdapter().publish(
        text="Apraksts", link="", images=[str(video)], fmt="reel")
    assert out == "v9"
    assert uploads[0][1] == b"mp4-bytes"
    assert uploads[0][2]["offset"] == "0"
    assert graph_calls[-1][1]["upload_phase"] == "finish"
    assert graph_calls[-1][1]["video_state"] == "PUBLISHED"


def test_article_video_extraction():
    from app.reels import article_video

    a = Article(guid="v-1", url="u", canonical_url="u", title="T", section="news",
                raw_json={"video_url": "https://cdn/klips.mp4"})
    assert article_video(a) == "https://cdn/klips.mp4"
    b = Article(guid="v-2", url="u", canonical_url="u", title="T", section="news",
                raw_json={"video": {"url": "https://cdn/k2.mp4"}})
    assert article_video(b) == "https://cdn/k2.mp4"
    c = Article(guid="v-3", url="u", canonical_url="u", title="T", section="news",
                raw_json={"video": "ne-url"})
    assert article_video(c) == ""


def test_resolve_format_prefers_real_video(session, monkeypatch):
    from app import pipeline, reels

    monkeypatch.setattr(reels, "available", lambda: True)
    def _video(url, out_dir=None, report=None, **kw):
        if report is not None:      # klipa lente ziņo, vai avotā bija skaņa
            report.update({"kind": "video_reel", "voiced": True, "source_audio": True})
        return "/data/cards/reel_v.mp4"

    monkeypatch.setattr(reels, "build_video_reel", _video)
    monkeypatch.setattr(reels, "build_reel",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError(
                            "slideshow nedrīkst būvēties, ja ir īsts video")))
    a = _article(session, "v-4")
    a.raw_json = {"video_url": "https://cdn/klips.mp4"}
    cfg = {"formats": ["photo", "reel"], "platform": "instagram"}
    fmt, media, _r = pipeline.resolve_format(session, "ig", cfg, a,
                                         {"format": "reel", "card_points": []})
    assert fmt == "reel"
    assert media == ["/data/cards/reel_v.mp4"]


def test_upsert_refreshes_video_but_keeps_caches(session):
    from app.ingest import upsert_article

    base = dict(guid="v-5", url="https://tv3.lv/v5", canonical_url="https://tv3.lv/v5",
                title="T", lead="L", categories=[], term_ids=[], images=[],
                published_at=None, editor_status="can", editor_timeframe="",
                section="news", feed_name="f", raw_json={})
    article, created, _ = upsert_article(session, dict(base))
    assert created
    article.raw_json = {"_img_wh": [100, 200]}
    session.flush()
    updated = dict(base, raw_json={"video_url": "https://cdn/k.mp4"})
    article, created, _ = upsert_article(session, updated)
    assert not created
    assert article.raw_json["video_url"] == "https://cdn/k.mp4"
    assert article.raw_json["_img_wh"] == [100, 200]


def _synthetic_video(path, ffmpeg, with_audio=True):
    import subprocess

    args = [ffmpeg, "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=3:size=540x960:rate=25"]
    if with_audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440:duration=3"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if with_audio:
        args += ["-c:a", "aac", "-shortest"]
    subprocess.run(args + [str(path)], check=True, capture_output=True)


def test_build_video_reel_end_to_end(monkeypatch, tmp_path):
    import os

    from app import reels

    try:
        import imageio_ffmpeg

        monkeypatch.setenv("FFMPEG_BIN", imageio_ffmpeg.get_ffmpeg_exe())
    except ImportError:
        pass
    if not os.environ.get("PLAYWRIGHT_CHROMIUM") and os.path.exists("/opt/pw-browsers/chromium"):
        monkeypatch.setenv("PLAYWRIGHT_CHROMIUM", "/opt/pw-browsers/chromium")
    if not reels.available():
        pytest.skip("ffmpeg or Chromium unavailable")
    src = tmp_path / "src.mp4"
    _synthetic_video(src, reels.ffmpeg_bin(), with_audio=True)
    out = reels.build_video_reel(str(src), out_dir=tmp_path)
    path = tmp_path / out.split("/")[-1]
    assert path.exists() and path.stat().st_size > 10000
    # klusa avota video arī iziet cauri (CTA kadram vajag saskanīgu audio)
    src2 = tmp_path / "src2.mp4"
    _synthetic_video(src2, reels.ffmpeg_bin(), with_audio=False)
    out2 = reels.build_video_reel(str(src2), out_dir=tmp_path)
    assert (tmp_path / out2.split("/")[-1]).exists()


def test_build_reel_end_to_end(monkeypatch, tmp_path):
    import os

    from app import reels

    try:
        import imageio_ffmpeg

        monkeypatch.setenv("FFMPEG_BIN", imageio_ffmpeg.get_ffmpeg_exe())
    except ImportError:
        pass
    if not os.environ.get("PLAYWRIGHT_CHROMIUM") and os.path.exists("/opt/pw-browsers/chromium"):
        monkeypatch.setenv("PLAYWRIGHT_CHROMIUM", "/opt/pw-browsers/chromium")
    if not reels.available():
        pytest.skip("ffmpeg or Chromium unavailable")
    out = reels.build_reel("Traģēdija Nepālā kļūst arvien lielāka", "news", "",
                           ["Pazudušo saraksts aug", "Latvieši starp meklētajiem"],
                           out_dir=tmp_path)
    path = tmp_path / out.split("/")[-1]
    assert path.exists() and path.stat().st_size > 10000
    assert path.suffix == ".mp4"


# --- ieruna (voice-over) ---------------------------------------------------

def _synthetic_voice(path, ffmpeg, seconds=12):
    """Runas vietā sinuss — mums svarīgs ir garums, nevis saturs."""
    import subprocess

    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"sine=frequency=220:duration={seconds}",
                    "-c:a", "aac", str(path)], check=True, capture_output=True)


def test_voice_script_prepares_text_for_speaking():
    from app import reels

    raw = ("Rīgā (Bauskas ielā) notika sprādziens. Vairāk lasi "
           "https://tv3.lv/zinas/kas-notika — tur ir viss stāsts par namu.")
    script = reels.voice_script(raw)
    assert "https://" not in script and "(" not in script
    assert "Bauskas ielā notika sprādziens." in script
    assert "  " not in script


def test_voice_script_cuts_at_a_sentence():
    from app import reels

    long_text = "Šis ir viens pilns teikums ar faktiem. " * 12
    script = reels.voice_script(long_text, max_words=20)
    assert script.endswith(".")
    assert len(script.split()) <= 20


def test_voice_script_rejects_a_stub():
    from app import reels

    assert reels.voice_script("Par īsu.") == ""
    assert reels.voice_script("") == ""


def test_frames_stretch_to_the_narration():
    from app import reels

    # 3 kadri x 2.8 s = 8.4 s video pret 14 s runu -> kadri aug proporcionāli
    stretched = reels._stretch_to_voice([2.8, 2.8, 2.8], 14.0)
    assert sum(stretched) == pytest.approx(14.0 + reels.VOICE_TAIL_SECONDS)
    assert stretched[0] == pytest.approx(stretched[2])   # CTA nestāv viens
    # īsa ieruna kadrus nesaīsina — teksts kadrā tāpat jāpaspēj izlasīt
    assert reels._stretch_to_voice([2.8, 2.8], 3.0) == [2.8, 2.8]
    # un neaug bez gala
    assert sum(reels._stretch_to_voice([2.8, 2.8], 300.0)) <= reels.VOICE_MAX_SECONDS


def test_build_reel_with_voice_end_to_end(monkeypatch, tmp_path):
    import os

    from app import reels

    try:
        import imageio_ffmpeg

        monkeypatch.setenv("FFMPEG_BIN", imageio_ffmpeg.get_ffmpeg_exe())
    except ImportError:
        pass
    if not os.environ.get("PLAYWRIGHT_CHROMIUM") and os.path.exists("/opt/pw-browsers/chromium"):
        monkeypatch.setenv("PLAYWRIGHT_CHROMIUM", "/opt/pw-browsers/chromium")
    if not reels.available():
        pytest.skip("ffmpeg or Chromium unavailable")

    voice = tmp_path / "voice.m4a"
    _synthetic_voice(voice, reels.ffmpeg_bin(), seconds=12)
    assert reels.media_duration(voice) == pytest.approx(12, abs=0.5)

    out = reels.build_reel("Kas zināms par Bauskas ielas namu", "news", "",
                           ["Jumts daļēji iebruka", "Lēmums vēl nav pieņemts"],
                           out_dir=tmp_path, voice=voice)
    path = tmp_path / out.split("/")[-1]
    assert path.exists() and path.stat().st_size > 10000
    # video ir izstiepts, lai ieruna izskanētu līdz galam
    assert reels.media_duration(path) >= 12


# --- kadru drošā zona un attēli --------------------------------------------

def test_cover_keeps_the_headline_inside_the_zoom_crop():
    """Ken Burns apgriež malas; virsraksta plāksne tur nedrīkst atrasties."""
    from app import cards, reels

    # cik pikseļu no 1080 platuma apgriež pie maksimālā tuvinājuma
    cropped = (1 - 1 / reels.MAX_ZOOM) / 2 * 1080
    assert cropped < reels.SAFE_INSET, "atkāpe mazāka par apgriezumu"

    doc = cards.build_story_html("Tukšas ielas, sprādzieni", "news",
                                 "https://cdn/x.jpg", inset=reels.SAFE_INSET)
    assert f"left:{reels.SAFE_INSET}px" in doc          # plāksne
    assert f"right:{48 + reels.SAFE_INSET}px" in doc    # tv3.lv logo


def test_static_story_is_not_indented():
    """Statisku stāstu neviens netuvina — tur plāksne paliek pie malas."""
    from app import cards

    doc = cards.build_story_html("Virsraksts", "news", "https://cdn/x.jpg")
    assert "left:0px" in doc and "right:48px" in doc


def test_point_frames_sit_inside_the_safe_zone():
    from app import reels

    doc = reels._point_frame_html("news", 1, "Pirmais fakts")
    assert f"left:{reels.SAFE_INSET + 72}px" in doc
    assert f"right:{reels.SAFE_INSET + 48}px" in doc


def test_reel_cover_falls_back_to_a_clean_photo(session):
    """Gatava photopost grafika vākam neder, bet tukšs vāks ir sliktāks."""
    from app import pipeline
    from app.models import Article

    a = Article(guid="ub-1", url="u", canonical_url="u", title="T",
                section="news", raw_json={},
                images=["https://cdn/uploads/photopost-graf.jpg",
                        "https://cdn/uploads/istais-foto.jpg"])
    session.add(a)
    session.flush()
    assert pipeline.unbranded_image(a) == "https://cdn/uploads/istais-foto.jpg"


def test_reel_cover_is_empty_only_when_every_image_is_prebranded(session):
    from app import pipeline
    from app.models import Article

    a = Article(guid="ub-2", url="u", canonical_url="u", title="T",
                section="news", raw_json={},
                images=["https://cdn/uploads/photopost-a.jpg",
                        "https://cdn/uploads/photopost-b.jpg"])
    session.add(a)
    session.flush()
    assert pipeline.unbranded_image(a) == ""


def test_frames_wait_for_images_before_the_screenshot():
    """Fiksēts miegs bija par īsu lēnam CDN — kadrs sanāca bez foto."""
    import inspect

    from app import reels

    src = inspect.getsource(reels._render_frames)
    assert "_settle" in src
    assert "wait_for_timeout(600)" not in src


# --- sadaļu lente ar ierunu -------------------------------------------------

def test_resolve_format_builds_a_section_reel_with_narration(session,
                                                             monkeypatch):
    """Sadaļas kļūst par kadriem, un bez atsevišķa scenārija balss nolasa
    tieši to, kas rakstīts kadros."""
    from app import pipeline, reels, tts
    from app.models import Article

    monkeypatch.setattr(reels, "available", lambda: True)
    spoken = {}
    monkeypatch.setattr(tts, "synthesize",
                        lambda text, **kw: spoken.update(text=text)
                        or "/audio/voice.mp3")
    built = {}

    def fake_build(title, section, image, points, out_dir=None, voice=None,
                   sections=None, point_images=None, report=None, **kw):
        built.update(sections=sections, voice=voice,
                     point_images=point_images, **kw)
        if report is not None:
            report.update(voiced=True, seconds=21.0,
                          narration=["Vētra nāk."]
                          + [s["body"] for s in (sections or [])])
        return "/data/cards/reel_s.mp4"

    monkeypatch.setattr(reels, "build_reel", fake_build)
    a = Article(guid="sr-reel", url="u", canonical_url="u", title="Vētra nāk",
                section="news", raw_json={},
                images=["https://cdn/foto1.jpg", "https://cdn/foto2.jpg"])
    session.add(a)
    session.flush()

    fmt, media, recipe = pipeline.resolve_format(
        session, "ig", {"formats": ["reel"], "platform": "instagram"}, a,
        {"format": "reel",
         "card_sections": [
             {"title": "Spēcīgas brāzmas", "body": "Vēja ātrums var sasniegt "
              "trīsdesmit metrus sekundē, īpaši piekrastē un Rīgā."},
             {"title": "Palikt mājās", "body": "Iedzīvotāji aicināti bez "
              "vajadzības neiziet un nostiprināt priekšmetus pagalmos."}]})

    assert fmt == "reel"
    assert built["sections"][0]["title"] == "Spēcīgas brāzmas"
    assert built["point_images"] == ["https://cdn/foto1.jpg",
                                     "https://cdn/foto2.jpg"]
    # ieruna vairs nav viens fails pār visu lenti — to sintezē build_reel
    # pa kadriem, tāpēc cauruļvads padod atklāšanas un noslēguma rindas
    assert built["voice"] is None
    assert built["cover_voice"] == "Vētra nāk."
    assert "tv3.lv" in built["end_voice"]
    assert recipe["voiced"] is True
    assert "trīsdesmit metrus" in recipe["voice_script"]
    # kadra virsraksts ierunā vairs neatkārtojas
    assert "Spēcīgas brāzmas." not in recipe["voice_script"]


def test_section_frames_take_longer_than_point_frames(monkeypatch, tmp_path):
    """Sadaļas kadrā ir teikumi — 2.8 s tos nevar izlasīt."""
    from pathlib import Path

    from app import reels

    seen = {}

    def fake_assemble(frames, workdir, out, frame_seconds=2.8, durations=None,
                      voice=None, voices=None, kinds=None):
        seen["durations"] = durations
        Path(out).write_bytes(b"mp4")
        return sum(durations)

    monkeypatch.setattr(reels, "_assemble", fake_assemble)
    monkeypatch.setattr(reels, "_render_frames",
                        lambda docs, out_dir: [tmp_path / f"f{i}.png"
                                               for i in range(len(docs))])
    reels.build_reel("T", "news", "", [],
                     sections=[{"title": "A", "body": "B garāks teksts"},
                               {"title": "C", "body": "D garāks teksts"}],
                     out_dir=tmp_path)
    # vāks + 2 sadaļas + CTA; sadaļu kadri ir vismaz SECTION_FRAME_SECONDS
    assert len(seen["durations"]) == 4
    assert seen["durations"][1] == reels.SECTION_FRAME_SECONDS
    assert seen["durations"][2] == reels.SECTION_FRAME_SECONDS


# --- ieruna un kadri iet kopsolī --------------------------------------------

def test_frame_length_follows_its_own_narration_not_a_global_stretch():
    """Kadru nosaka TĀ PAŠA kadra ieruna.

    Vecais ceļš stiepa visus kadrus proporcionāli vienam runas gabalam. Ja
    otrā nodaļa runāja divreiz ilgāk par pirmo, attēls tik un tā mainījās uz
    pusēm — un lentes beigās CTA kadrs jau stāvēja, kamēr balss vēl stāstīja
    iepriekšējo nodaļu. Tieši to pamanīja redakcija.
    """
    from app import reels

    base = [2.8, 5.5, 5.5, 2.8]
    voices = ["c.mp3", "a1.mp3", "a2.mp3", "e.mp3"]
    speech = [2.0, 4.0, 12.0, 3.0]     # otrā nodaļa runā trīsreiz ilgāk
    out = reels.plan_durations(base, voices, speech)

    for planned, spoken in zip(out, speech):
        assert planned >= spoken + reels.VOICE_LEAD_SECONDS
    # garā nodaļa dabū savu laiku, nevis vidējo daļu no kopsummas
    assert out[2] > out[1] * 2
    # un neviens kadrs nepazib garām
    assert min(out) >= reels.MIN_FRAME_SECONDS


def test_a_frame_without_narration_keeps_its_planned_length():
    from app import reels

    out = reels.plan_durations([2.8, 5.5, 2.8], ["", "a.mp3", ""], [0.0, 4.0, 0.0])
    assert out[0] == 2.8 and out[2] == 2.8
    assert out[1] >= 4.0


def test_narration_skips_the_chapter_headline():
    """Virsraksts ir uz ekrāna; nolasot to vēlreiz, doma atkārtojas."""
    from app import reels

    sec = {"title": "NATO reakcija",
           "body": "Meklēšanā iesaistīti NATO iznīcinātāji."}
    assert reels.chapter_voice(sec) == "Meklēšanā iesaistīti NATO iznīcinātāji."

    # arī tad, kad AI pats teksta sākumā virsrakstu atkārto
    doubled = {"title": "NATO reakcija",
               "body": "NATO reakcija: meklēšanā iesaistīti Baltijas gaisa "
                       "telpas patrulēšanas misijas iznīcinātāji."}
    spoken = reels.chapter_voice(doubled)
    assert spoken.startswith("meklēšanā iesaistīti")


def test_every_frame_carries_its_own_narration():
    """Teksti un kadri vairs nav divi paralēli saraksti, ko jātur vienā
    garumā — katrs kadrs nes savu rindu, tāpēc nesakrist nav kur."""
    from app import reels

    secs = [{"title": "A", "body": "Pirmais teksts."},
            {"title": "B", "body": "Otrais teksts."}]
    beats = reels.plan_beats("Virsraksts", secs, [], cover_voice="Āķis.",
                             end_voice="Beigas.")
    assert [b["kind"] for b in beats] == ["cover", "section", "section", "end"]
    assert [b["text"] for b in beats] == ["Āķis.", "Pirmais teksts.",
                                          "Otrais teksts.", "Beigas."]

    # bez vāka un bez CTA kadra paliek tikai saturs
    bare = reels.plan_beats("Virsraksts", secs, [], include_cover=False,
                            include_end=False)
    assert [b["text"] for b in bare] == ["Pirmais teksts.", "Otrais teksts."]


def test_a_too_long_reel_loses_whole_chapters_not_half_sentences():
    from app import reels

    beats = [{"kind": "cover", "duration": 3.0},
             {"kind": "section", "duration": 30.0},
             {"kind": "section", "duration": 30.0},
             {"kind": "section", "duration": 30.0},
             {"kind": "end", "duration": 3.0}]
    dropped = reels._trim_beats(beats)
    assert dropped >= 1
    assert beats[0]["kind"] == "cover" and beats[-1]["kind"] == "end"
    assert sum(b["duration"] for b in beats) <= reels.VOICE_MAX_SECONDS


def test_the_end_frame_is_marked_as_ai_but_does_not_say_it_out_loud():
    """Marķējums ir redzams; izrunāts tas nāca kā liekais teikums aiz
    aicinājuma. Kam vajag arī skaļi — noteikums to atgriež atpakaļ."""
    from app import reels

    said = reels.end_voice_text({})
    assert "tv3.lv" in said
    assert "mākslīg" not in said.lower()
    assert "MI" in reels._end_frame_html({})

    louder = reels.end_voice_text({"ai_disclosure_spoken": "Sagatavoja MI."})
    assert louder.endswith("Sagatavoja MI.")


# --- lente nekad nepaliek bez attēla ----------------------------------------

def test_reel_falls_back_to_a_blurred_graphic_instead_of_a_flat_colour():
    """Rakstā ir TIKAI photopost grafika: zem plāksnes tā neder, bet
    izpludināta der — un tieši tā trūka lentēs, kur foto nebija nemaz."""
    from app import cards, reels

    graphic = "https://cdn/photopost-1.jpg"
    cover = cards.build_story_html("Virsraksts", "news", "",
                                   inset=reels.SAFE_INSET, blur_image=graphic,
                                   ai_badge=True)
    assert "bgblur" in cover and graphic in cover

    frame = reels._section_frame_html("news", 1, "Nodaļa", "Teksts.",
                                      bg_image="", blur_image=graphic, total=3)
    assert "blurbg" in frame and graphic in frame


def test_a_real_photo_beats_the_blurred_fallback():
    from app import reels

    frame = reels._section_frame_html("news", 1, "Nodaļa", "Teksts.",
                                      bg_image="https://cdn/foto.jpg",
                                      blur_image="https://cdn/photopost.jpg",
                                      total=3)
    assert "https://cdn/foto.jpg" in frame
    assert "photopost" not in frame


def test_section_frames_show_how_far_the_story_has_got():
    from app import reels

    frame = reels._section_frame_html("news", 2, "Nodaļa", "Teksts.", total=3)
    assert frame.count('<i class="on">') == 2
    assert frame.count("<i class=") == 3
    # viena nodaļa vien nav progress — joslu tad nezīmējam
    assert 'class="prog"' not in reels._section_frame_html(
        "news", 1, "N", "T.", total=1)


def test_per_frame_narration_end_to_end_keeps_video_and_voice_together(
        monkeypatch, tmp_path):
    """Īstais pierādījums: uzbūvēta lente, kuras garums ir tieši tās kadru
    summa, un katrs kadrs ir tik garš, cik tā ieruna.

    Nodaļas te runā ļoti dažādi gari (2 s un 9 s). Ar veco proporcionālo
    stiepšanu abi kadri iznāktu vienāda garuma, un balss pāri kadru robežai
    aizietu — tieši tas, ko redakcija redzēja lentē.
    """
    import os

    from app import reels

    try:
        import imageio_ffmpeg

        monkeypatch.setenv("FFMPEG_BIN", imageio_ffmpeg.get_ffmpeg_exe())
    except ImportError:
        pass
    if not os.environ.get("PLAYWRIGHT_CHROMIUM"):
        for cand in ("/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
                     "/opt/pw-browsers/chromium"):
            if os.path.exists(cand):
                monkeypatch.setenv("PLAYWRIGHT_CHROMIUM", cand)
                break
    if not reels.available():
        pytest.skip("ffmpeg or Chromium unavailable")

    lengths = {"cover": 2.0, "s1": 2.0, "s2": 9.0, "end": 3.0}
    made: dict[str, float] = {}

    def fake_synth(text, **kw):
        key = ("cover" if text.startswith("Vētra") else
               "end" if "tv3.lv" in text else
               "s1" if text.startswith("Vēja") else "s2")
        path = tmp_path / f"{key}.m4a"
        if not path.exists():
            _synthetic_voice(path, reels.ffmpeg_bin(), seconds=lengths[key])
        made[key] = lengths[key]
        return str(path)

    report: dict = {}
    out = reels.build_reel(
        "Vētra nāk", "news", "", [], out_dir=tmp_path,
        sections=[{"title": "Brāzmas", "body": "Vēja ātrums pieaug."},
                  {"title": "Ieteikumi", "body": "Neizejiet bez vajadzības."}],
        cover_voice="Vētra nāk pār Latviju.",
        end_voice=reels.end_voice_text({}),
        synth=fake_synth, report=report)

    path = tmp_path / out.split("/")[-1]
    assert path.exists() and path.stat().st_size > 10000
    assert report["voiced"] is True and report["frames"] == 4
    assert made == lengths          # katram kadram sintezēts savs gabals

    expected = sum(reels.frame_seconds_for(lengths[k], last=(k == "end"),
                                           cover=(k == "cover"))
                   for k in ("cover", "s1", "s2", "end"))
    assert reels.media_duration(path) == pytest.approx(expected, abs=0.6)
    # garā nodaļa aizņem savu vietu, nevis vidējo daļu
    assert reels.frame_seconds_for(9.0) > 2 * reels.frame_seconds_for(2.0)


# --- ievads, progress un marķējums pēc redakcijas atsauksmēm ----------------

def test_the_cover_reads_the_headline_and_nothing_else(session, monkeypatch):
    """AI rakstītais āķis bija gan garš, gan saturiski tas pats, ko pirmā
    nodaļa — divas reizes viena doma, pirms stāsts vispār sācies."""
    from app import pipeline, reels
    from app.models import Article

    monkeypatch.setattr(reels, "available", lambda: True)
    built = {}

    def fake_build(title, section, image, points, report=None, **kw):
        built.update(kw)
        return "/data/cards/reel_i.mp4"

    monkeypatch.setattr(reels, "build_reel", fake_build)
    a = Article(guid="iv-1", url="u", canonical_url="u", raw_json={},
                title="Saka vārti nodrošina uzvaru Birmingemā", section="sport",
                images=["https://cdn/f.jpg"])
    session.add(a)
    session.flush()

    pipeline.resolve_format(
        session, "ig", {"formats": ["reel"], "platform": "instagram"}, a,
        {"format": "reel",
         # pat ja modelis āķi tomēr atsūta, vāks to nelieto
         "voice_script": "Šis ir garš ievads par to pašu, kas nāk nākamajā kadrā.",
         "card_sections": [
             {"title": "Vārti", "body": "Bukajo Saka guva vienīgos vārtus."},
             {"title": "Sekas", "body": "Komanda izcīnīja pirmo uzvaru izbraukumā."}]})

    assert built["cover_voice"] == "Saka vārti nodrošina uzvaru Birmingemā."


def test_the_progress_bar_counts_the_frames_the_viewer_actually_sees():
    """«1 no 3» otrajā kadrā no pieciem ir maldinoši — skatītājs skaita to,
    ko redz, nevis to, ko mēs saucam par saturu."""
    from app import reels

    beats = reels.plan_beats(
        "T", [{"title": "A", "body": "Pirmais."}, {"title": "B", "body": "Otrais."}],
        [], cover_voice="Virsraksts.", end_voice="Lasi tv3.lv.")
    html = reels._beat_html(beats[1], 2, len(beats), "news", "T", "", "",
                            None, None)
    assert html.count("<i class=") == 4        # vāks + 2 nodaļas + CTA
    assert html.count('<i class="on">') == 2   # esam otrajā kadrā


def test_a_trimmed_reel_does_not_promise_chapters_it_no_longer_has(monkeypatch,
                                                                   tmp_path):
    """Kadrus zīmējām PIRMS apgriešanas, tāpēc izdzīvojušie nesa veco
    kopskaitu — josla solīja nodaļas, kuru lentē vairs nebija."""
    from pathlib import Path

    from app import reels

    rendered = {}

    def fake_render(docs, out_dir):
        rendered["docs"] = docs
        return [tmp_path / f"f{i}.png" for i in range(len(docs))]

    def fake_assemble(frames, workdir, out, frame_seconds=2.8, durations=None,
                      voice=None, voices=None, kinds=None):
        Path(out).write_bytes(b"mp4")
        return sum(durations)

    monkeypatch.setattr(reels, "_render_frames", fake_render)
    monkeypatch.setattr(reels, "_assemble", fake_assemble)
    # katra nodaļa runā 25 s -> 60 s budžetā visas trīs neietilpst
    monkeypatch.setattr(reels, "media_duration", lambda p: 25.0)

    report: dict = {}
    reels.build_reel(
        "T", "news", "", [], out_dir=tmp_path,
        sections=[{"title": "A", "body": "Pirmais teksts."},
                  {"title": "B", "body": "Otrais teksts."},
                  {"title": "C", "body": "Trešais teksts."}],
        cover_voice="Virsraksts.", end_voice="Lasi tv3.lv.",
        synth=lambda text, **kw: "/audio/x.m4a", report=report)

    docs = rendered["docs"]
    assert report["frames"] == len(docs) < 5          # kaut kas ir izmests
    for doc in docs:
        if 'class="prog"' in doc:
            assert doc.count("<i class=") == len(docs)


def test_only_a_voiced_reel_is_marked_as_ai(monkeypatch, tmp_path):
    """Sintezētā balss ir vienīgā daļa, kas tiešām ir mākslīgi ģenerēts
    medijs. Klusā lentē ir foto un teksts no žurnālista raksta, un zīmīte tur
    lasās kā apgalvojums, ka MI ir uzrakstījis rakstu."""
    from pathlib import Path

    from app import disclosure, reels

    rendered = {}

    def fake_render(docs, out_dir):
        rendered["docs"] = docs
        return [tmp_path / f"f{i}.png" for i in range(len(docs))]

    def fake_assemble(frames, workdir, out, frame_seconds=2.8, durations=None,
                      voice=None, voices=None, kinds=None):
        Path(out).write_bytes(b"mp4")
        return sum(durations)

    monkeypatch.setattr(reels, "_render_frames", fake_render)
    monkeypatch.setattr(reels, "_assemble", fake_assemble)
    monkeypatch.setattr(reels, "media_duration", lambda p: 3.0)
    sections = [{"title": "A", "body": "Pirmais teksts."},
                {"title": "B", "body": "Otrais teksts."}]

    reels.build_reel("T", "news", "", [], out_dir=tmp_path, sections=sections,
                     cover_voice="Virsraksts.", end_voice="Lasi tv3.lv.",
                     synth=lambda text, **kw: "/audio/x.m4a")
    assert any(disclosure.DEFAULT_SHORT in d for d in rendered["docs"])
    assert any(disclosure.DEFAULT_TEXT in d for d in rendered["docs"])

    # bez balss (nav atslēgas / sintēze neizdevās) marķējuma nav
    reels.build_reel("T", "news", "", [], out_dir=tmp_path, sections=sections,
                     cover_voice="Virsraksts.", end_voice="Lasi tv3.lv.",
                     synth=lambda text, **kw: "")
    assert not any(disclosure.DEFAULT_SHORT in d for d in rendered["docs"])
    assert not any(disclosure.DEFAULT_TEXT in d for d in rendered["docs"])


def test_the_report_says_how_long_the_voice_actually_speaks(monkeypatch, tmp_path):
    """Tempu vārdos minūtē var izrēķināt tikai no runas garuma. Lentes
    kopgarums tam neder: tajā ir arī klusumi un CTA kadrs."""
    from pathlib import Path

    from app import reels

    def fake_assemble(frames, workdir, out, frame_seconds=2.8, durations=None,
                      voice=None, voices=None, kinds=None):
        Path(out).write_bytes(b"mp4")
        return sum(durations)

    monkeypatch.setattr(reels, "_render_frames",
                        lambda docs, out_dir: [tmp_path / f"f{i}.png"
                                               for i in range(len(docs))])
    monkeypatch.setattr(reels, "_assemble", fake_assemble)
    monkeypatch.setattr(reels, "media_duration", lambda p: 4.0)

    report: dict = {}
    reels.build_reel("T", "news", "", [], out_dir=tmp_path,
                     sections=[{"title": "A", "body": "Pirmais teksts."},
                               {"title": "B", "body": "Otrais teksts."}],
                     cover_voice="Virsraksts.", end_voice="Beigas.",
                     synth=lambda text, **kw: "/a.m4a", report=report)
    # četri kadri, katrs ar 4 s runas
    assert report["speech_seconds"] == 16.0
    # kopgarums ir lielāks: klusumi un elpas nāk virsū
    assert report["seconds"] > report["speech_seconds"]


def test_the_report_says_which_voice_and_pace_were_actually_used(tmp_path,
                                                                 monkeypatch):
    """Sadaļas balss un temps nāk no diviem noteikumiem, un Noteikumu failā
    piemērs ir komentārs — izkomentēta rinda izskatās pēc iestatījuma. Tāpēc
    receptē jāpaliek rezultātam: ar KO tas tika ierunāts, ne ar ko bija
    domāts."""
    from pathlib import Path

    from app import reels

    def fake_assemble(frames, workdir, out, frame_seconds=2.8, durations=None,
                      voice=None, voices=None, kinds=None):
        Path(out).write_bytes(b"mp4")
        return sum(durations)

    monkeypatch.setattr(reels, "_render_frames",
                        lambda docs, out_dir: [tmp_path / f"f{i}.png"
                                               for i in range(len(docs))])
    monkeypatch.setattr(reels, "_assemble", fake_assemble)
    monkeypatch.setattr(reels, "media_duration", lambda p: 4.0)

    rules = {"tts_provider": "elevenlabs", "reel_voice_name": "female",
             "reel_voice_rate": -4,
             "reel_voice_by_section": {"entertainment": "izklaides-balss"},
             "reel_voice_rate_by_section": {"entertainment": 12}}

    def build(section):
        report: dict = {}
        reels.build_reel("T", section, "", [], out_dir=tmp_path, rules=rules,
                         sections=[{"title": "A", "body": "Teksts."}],
                         cover_voice="Virsraksts.",
                         synth=lambda text, **kw: "/a.m4a", report=report)
        return report

    fun = build("entertainment")
    assert fun["voice_used"] == "izklaides-balss" and fun["voice_rate"] == 12
    assert fun["voice_by_section"] and fun["rate_by_section"]

    # sadaļa bez savas rindas dabū kopīgo — un to arī pasaka
    news = build("news")
    assert news["voice_used"] == "21m00Tcm4TlvDq8ikWAM"
    assert news["voice_rate"] == -4
    assert not news["voice_by_section"] and not news["rate_by_section"]


def test_a_short_title_does_not_make_the_cover_flash_past():
    """Vāks runā tikai virsrakstu — divas sekundes. Bet virsraksts ir
    lielākais teksts lentē, un to vēl arī jālasa: kadrs, kas pazūd līdz ar
    pēdējo izrunāto vārdu, ir kadrs, kuru neviens neizlasīja."""
    from app import reels

    short = 1.6                         # «Vētra nāk pār Latviju.»
    assert reels.frame_seconds_for(short) < reels.COVER_MIN_SECONDS
    assert reels.frame_seconds_for(short, cover=True) == reels.COVER_MIN_SECONDS
    # garam virsrakstam grīda netraucē — kadrs ir tik garš, cik runa
    assert reels.frame_seconds_for(6.0, cover=True) \
        == reels.frame_seconds_for(6.0)
    # un plānā vāks to dabū, nodaļa ar tikpat īsu runu — ne
    out = reels.plan_durations([2.8, 2.8], ["/a", "/b"], [short, short],
                               kinds=["cover", "section"])
    assert out[0] == reels.COVER_MIN_SECONDS
    assert out[1] == reels.frame_seconds_for(short, last=True)


# --- TTS budžets: maksājam tikai par to, kas lentē tiešām skan -------------

def test_spoken_head_keeps_short_text_and_cuts_long_at_a_sentence():
    from app import reels

    short = "Viens teikums."
    assert reels.spoken_head(short, 220) == short

    long = ("Pirmais teikums ir īss. Otrais teikums ir mazliet garāks nekā "
            "pirmais. Trešais teikums vairs neietilps, jo limits ir mazs.")
    out = reels.spoken_head(long, 80)
    assert out == "Pirmais teikums ir īss. Otrais teikums ir mazliet garāks nekā pirmais."
    # pirmais teikums paliek arī tad, ja tas viens pats pārsniedz limitu
    assert reels.spoken_head(long, 10) == "Pirmais teikums ir īss."


def test_spoken_head_does_not_split_ordinal_numbers():
    """Latviski «59. minūtē» ir kārtas skaitlis, ne teikuma beigas."""
    from app import reels

    text = "Vārti krita 59. minūtē pēc stūra sitiena. Otrais teikums nāk vēlāk."
    assert reels.spoken_head(text, 45) == "Vārti krita 59. minūtē pēc stūra sitiena."


def test_chapter_voice_reads_only_the_core_of_the_chapter():
    from app import reels

    body = " ".join(f"Teikums numur {i} ar dažiem papildu vārdiem iekšā." for i in range(8))
    sec = {"title": "Nodaļa", "body": body}
    assert len(reels.chapter_voice(sec)) <= reels.CHAPTER_VOICE_CHARS
    assert reels.chapter_voice(sec).endswith(".")
    # noteikums maina griezumu; redaktora dotu ierunu neaiztiekam
    assert len(reels.chapter_voice(sec, {"reel_chapter_voice_chars": 120})) <= 120
    assert reels.chapter_voice({"title": "N", "body": body, "voice": body}) == body


def test_voice_beats_skips_chapters_that_will_not_fit_before_synthesis(monkeypatch):
    """Trešā nodaļa agrāk tika ierunāta (un samaksāta) un tad izmesta.
    Tagad tā netiek sūtīta uz TTS vispār."""
    from app import reels

    monkeypatch.setattr(reels, "media_duration", lambda p: 20.0)
    calls: list[str] = []

    def synth(text, **kw):
        calls.append(text)
        return "/a.m4a"

    beats = reels.plan_beats(
        "T", [{"title": "A", "body": "Pirmais teksts."},
              {"title": "B", "body": "Otrais teksts."},
              {"title": "C", "body": "Trešais teksts."}], [],
        cover_voice="Virsraksts.", end_voice="Lasi tv3.lv.")
    voices, speech, skipped = reels.voice_beats(beats, synth, budget=60)

    assert skipped >= 1
    assert "Trešais teksts." not in calls
    assert "Pirmais teksts." in calls and "Lasi tv3.lv." in calls
    assert [b["kind"] for b in beats] == ["cover"] + ["section"] * (3 - skipped) + ["end"]
    assert len(voices) == len(speech) == len(beats)


def test_voice_beats_skips_the_rest_after_the_first_skipped_chapter(monkeypatch):
    """Nodaļas ir stāsts pēc kārtas: ja otrā neietilpst, trešo nesāk arī tad,
    ja tā būtu īsāka."""
    from app import reels

    monkeypatch.setattr(reels, "media_duration", lambda p: 30.0)
    calls: list[str] = []
    beats = reels.plan_beats(
        "T", [{"title": "A", "body": "Pirmais teksts, kas ir gana garš."},
              {"title": "B", "body": "Otrais teksts, arī gana garš, lai neietilptu."},
              {"title": "C", "body": "Īss."}], [],
        cover_voice="Virsraksts.", end_voice="Lasi.")
    reels.voice_beats(beats, lambda t, **kw: calls.append(t) or "/a.m4a", budget=60)

    assert "Īss." not in calls
    assert [b["kind"] for b in beats] == ["cover", "section", "end"]


def test_reel_report_counts_the_characters_sent_to_tts(monkeypatch, tmp_path):
    from pathlib import Path

    from app import reels

    monkeypatch.setattr(reels, "_render_frames",
                        lambda docs, out_dir: [tmp_path / f"f{i}.png" for i in range(len(docs))])

    def fake_assemble(frames, workdir, out, frame_seconds=2.8, durations=None,
                      voice=None, voices=None, kinds=None):
        Path(out).write_bytes(b"mp4")
        return sum(durations)

    monkeypatch.setattr(reels, "_assemble", fake_assemble)
    monkeypatch.setattr(reels, "media_duration", lambda p: 4.0)
    report: dict = {}
    reels.build_reel(
        "T", "news", "", [], out_dir=tmp_path,
        sections=[{"title": "A", "body": "Pirmais teksts."}],
        cover_voice="Virsraksts.", end_voice="Lasi.",
        synth=lambda text, **kw: "/a.m4a", report=report)
    assert report["voice_chars"] == len("Virsraksts.") + len("Pirmais teksts.") + len("Lasi.")


def test_silent_reel_records_why_instead_of_swallowing_it(monkeypatch, tmp_path):
    """Sintēze nemet kļūdu — klusa lente izskatās gluži kā apzināta izvēle.
    Iemeslam jāpaliek receptē, citādi «kāpēc nav skaņas» nav atbildams."""
    from app import reels

    monkeypatch.setattr(reels, "_render_frames",
                        lambda docs, wd: [str(tmp_path / "f.png")] * len(docs))
    monkeypatch.setattr(reels, "_assemble", lambda *a, **kw: 12.0)

    def failing(text, section="", errors=None, **kw):
        if errors is not None:
            errors.append("HTTP 401: balss ID nav šai atslēgai")
        return ""

    report: dict = {}
    reels.build_reel("Virsraksts", "sport", "", [],
                     out_dir=tmp_path,
                     sections=[{"title": "A", "body": "Pirmais teikums par notikumu."},
                               {"title": "B", "body": "Otrais teikums ar faktiem."}],
                     cover_voice="Virsraksts.", end_voice="Lasi tv3.lv.",
                     synth=failing, report=report)
    assert report["voiced"] is False
    assert "401" in " ".join(report["voice_errors"])


def test_diagnostics_names_the_silent_reels(session):
    """Diagnostikā redz, cik lenšu ir ar skaņu un kāpēc pārējās nav."""
    from app import diagnostics
    from app.models import Article, Post, utcnow

    a = Article(guid="g", url="https://tv3.lv/a", canonical_url="https://tv3.lv/a",
                title="T", section="sport", feed_name="tv3", editor_status="can",
                published_at=utcnow(), raw_json={})
    session.add(a)
    session.flush()
    session.add(Post(article_id=a.id, channel="fb", format="reel", copy="c",
                     state="published", scheduled_at=utcnow(),
                     extra={"recipe": {"voiced": True, "voice_used": "Nils"}}))
    session.add(Post(article_id=a.id, channel="fb", format="reel", copy="c",
                     state="published", scheduled_at=utcnow(),
                     extra={"recipe": {"voiced": False, "section": "sport",
                                       "voice_errors": ["HTTP 401: nederīga atslēga"]}}))
    session.add(Post(article_id=a.id, channel="fb", format="reel", copy="c",
                     state="published", scheduled_at=utcnow(),
                     extra={"recipe": {"kind": "video_reel", "voiced": False}}))
    session.flush()

    out = diagnostics._reel_voice(session)
    assert out["reels"] == 3 and out["voiced"] == 1
    assert out["voices"] == {"Nils": 1}
    reasons = " ".join(out["reasons"])
    assert "401" in reasons and "avota klipam nav skaņas" in reasons


def test_voice_check_tests_every_configured_voice(monkeypatch):
    """Redaktors izvēlējās savus balss ID, jo tie latviski skan labāk. Ja viens
    no tiem nav derīgs, tieši tās sadaļas lentes klusē — un no ārpuses tas
    izskatās tāpat kā apzināti klusa lente. Pārbaudei jāsauc katra balss."""
    from app import tts

    rules = {"reel_voice": True, "tts_provider": "elevenlabs",
             "reel_voice_name": "onwK4e9ZLuTAKqWW03F9",
             "reel_voice_by_section": {"sport": "cg5gspJ2msm6clMCkdW9",
                                       "news": "nederigs-id"}}
    monkeypatch.setattr(tts, "_key", lambda session=None, rules=None: "k")
    called = []

    def fake(text, voice, session, errors, r, rate):
        called.append(voice)
        if voice == "nederigs-id":
            errors.append("HTTP 404: voice_not_found")
            return b""
        return b"audio"

    monkeypatch.setitem(tts._SYNTHS, "elevenlabs", fake)
    out = tts.check_voices(rules=rules)
    assert out["provider"] == "elevenlabs" and out["key"] is True
    assert called == ["onwK4e9ZLuTAKqWW03F9", "nederigs-id", "cg5gspJ2msm6clMCkdW9"]
    assert out["broken"] == ["news"]
    bad = next(v for v in out["voices"] if v["section"] == "news")
    assert "voice_not_found" in bad["error"]


def test_voice_check_says_plainly_when_the_key_is_missing(monkeypatch):
    from app import tts

    monkeypatch.setattr(tts, "_key", lambda session=None, rules=None: "")
    out = tts.check_voices(rules={"reel_voice": True, "tts_provider": "elevenlabs"})
    assert out["key"] is False and "atslēgas nav" in out["note"]


def test_unknown_silence_is_not_reported_as_a_diagnosis(session):
    """Lente, kas būvēta pirms kļūdu uzskaites, nezina savu iemeslu. To
    nedrīkst pasniegt kā «nav atslēgas» — tas ir minējums, ne mērījums."""
    from app import diagnostics
    from app.models import Article, Post, utcnow

    a = Article(guid="g2", url="https://tv3.lv/b", canonical_url="https://tv3.lv/b",
                title="T", section="news", feed_name="tv3", editor_status="can",
                published_at=utcnow(), raw_json={})
    session.add(a)
    session.flush()
    session.add(Post(article_id=a.id, channel="fb", format="reel", copy="c",
                     state="published", scheduled_at=utcnow(),
                     extra={"recipe": {"voiced": False, "section": "news"}}))
    session.flush()
    out = diagnostics._reel_voice(session)
    assert "nav pierakstīts" in " ".join(out["reasons"])


def test_brand_is_spoken_as_one_word_without_a_pause():
    """«tv trīs» ar atstarpi ir vārda robeža, un runātājs tajā ietur pauzi —
    ausij tad skan «TV ... trīs», nevis kanāla vārds. Izrunas pierakstā zīmols
    ir viens vārds."""
    from app import tts

    assert tts.spoken_text("Pilnu stāstu lasi tv3.lv.", {}) == \
        "Pilnu stāstu lasi tēvētrīs punkts lv."
    assert tts.spoken_text("Skaties TV3 Play bez maksas.", {}) == \
        "Skaties tēvētrīs pleij bez maksas."
    # nekur vairs nav atstarpes zīmola vidū
    for value in tts.PRONUNCIATION.values():
        assert "tv trīs" not in value


def test_the_reel_closing_line_carries_the_fixed_brand():
    """Beigu kadra teikums ir tas, ko redakcija pamanīja — tas iet caur to pašu
    vārdnīcu, tāpēc labojums tur jāredz."""
    from app import reels, tts

    spoken = tts.spoken_text(reels.end_voice_text({}), {})
    assert "tēvētrīs" in spoken and "tv trīs" not in spoken
