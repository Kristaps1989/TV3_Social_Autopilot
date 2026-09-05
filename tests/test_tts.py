"""Balss sintēze reelu ierunai."""
from urllib.parse import unquote

import httpx
import pytest
from fastapi.testclient import TestClient

from app import credentials, tts
from app.main import app


@pytest.fixture()
def client(session):
    with TestClient(app) as c:
        c.post("/setup", data={"password": "slepens123", "password2": "slepens123"})
        yield c


@pytest.fixture()
def keyed(session):
    credentials.put(session, "azure_speech_key", "test-key")
    credentials.put(session, "azure_speech_region", "westeurope")
    return session


def test_ssml_breaks_between_sentences():
    doc = tts.build_ssml("Namā iebruka jumts. Lēmums vēl nav pieņemts.",
                         "lv-LV-NilsNeural")
    assert 'xml:lang="lv-LV"' in doc
    assert 'name="lv-LV-NilsNeural"' in doc
    assert doc.count("<break") == 1          # viena robeža starp diviem teikumiem
    assert "Namā iebruka jumts." in doc


def test_ssml_escapes_markup():
    doc = tts.build_ssml('Firma "A&B" <nedeva> komentāru un tas ir viss.')
    assert "<nedeva>" not in doc
    assert "&amp;" in doc and "&lt;nedeva&gt;" in doc


def test_voice_choice_from_rules():
    assert tts.voice_name({"reel_voice_name": "male"}) == "lv-LV-NilsNeural"
    assert tts.voice_name({"reel_voice_name": "female"}) == "lv-LV-EveritaNeural"
    assert tts.voice_name({}) == tts.DEFAULT_VOICE
    # pilns Azure nosaukums iet cauri kā ir
    assert tts.voice_name({"reel_voice_name": "lv-LV-NilsNeural"}) == "lv-LV-NilsNeural"


def test_silent_without_a_key(session, tmp_path):
    assert tts.enabled(session=session) is False
    assert tts.synthesize("Teksts, ko nekad nenolasīs.", tmp_path,
                          session=session) == ""


def test_disabled_by_rules_even_with_a_key(keyed, tmp_path, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _never_called())
    assert tts.synthesize("Teksts.", tmp_path, rules={"reel_voice": False},
                          session=keyed) == ""


def _never_called():  # pragma: no cover — tikai ja karogs tiek ignorēts
    raise AssertionError("Azure must not be called when reel_voice is off")


def test_synthesize_writes_mp3_and_caches(keyed, tmp_path, monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs["headers"], kwargs["content"]))
        return httpx.Response(200, content=b"ID3fake-mp3-bytes")

    monkeypatch.setattr(httpx, "post", fake_post)
    path = tts.synthesize("Namā iebruka jumts un lēmums vēl nav pieņemts.",
                          tmp_path, rules={"reel_voice": True}, session=keyed)

    assert path.endswith(".mp3")
    from pathlib import Path

    assert Path(path).read_bytes() == b"ID3fake-mp3-bytes"
    url, headers, content = calls[0]
    assert url.startswith("https://westeurope.tts.speech.microsoft.com/")
    assert headers["Ocp-Apim-Subscription-Key"] == "test-key"
    assert b"lv-LV" in content

    # tas pats teksts otrreiz nāk no keša, nevis no Azure
    again = tts.synthesize("Namā iebruka jumts un lēmums vēl nav pieņemts.",
                           tmp_path, rules={"reel_voice": True}, session=keyed)
    assert again == path and len(calls) == 1
    # cits teksts ir cits fails
    other = tts.synthesize("Pavisam cits teikums šoreiz.", tmp_path,
                           rules={"reel_voice": True}, session=keyed)
    assert other != path and len(calls) == 2


def test_failed_request_leaves_no_half_file(keyed, tmp_path, monkeypatch):
    monkeypatch.setattr(httpx, "post",
                        lambda *a, **k: httpx.Response(401, text="Unauthorized"))
    assert tts.synthesize("Teksts.", tmp_path, rules={"reel_voice": True},
                          session=keyed) == ""
    assert list(tmp_path.glob("*.mp3")) == []


def test_network_error_is_not_fatal(keyed, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx, "post", boom)
    assert tts.synthesize("Teksts.", tmp_path, rules={"reel_voice": True},
                          session=keyed) == ""


def test_reel_recipe_voice(keyed, tmp_path, monkeypatch):
    monkeypatch.setattr(httpx, "post",
                        lambda *a, **k: httpx.Response(200, content=b"mp3"))
    monkeypatch.setattr(tts, "synthesize",
                        lambda text, **kw: f"/audio/{len(text)}.mp3" if text else "")
    assert tts.reel_voice({"voice_script": "Teksts"}) == "/audio/6.mp3"
    assert tts.reel_voice({}) == ""
    assert tts.reel_voice(None) == ""


def test_connect_page_offers_the_voice_key(client, session):
    body = client.get("/connect").text
    assert "Reelu balss (Azure Speech)" in body
    assert "/connect/azure-speech" in body


def test_saving_a_key_reports_a_failed_sample(client, session, monkeypatch):
    monkeypatch.setattr(httpx, "post",
                        lambda *a, **k: httpx.Response(403, text="Forbidden"))
    r = client.post("/connect/azure-speech",
                    data={"api_key": "bad-key", "region": "narnia"},
                    follow_redirects=False)
    assert "/connect?error=" in r.headers["location"]
    # atslēga tomēr ir saglabāta, lai reģionu var izlabot bez pārrakstīšanas
    assert credentials.get("azure_speech_key", session) == "bad-key"


def test_saving_a_working_key_confirms(client, session, monkeypatch):
    monkeypatch.setattr(httpx, "post",
                        lambda *a, **k: httpx.Response(200, content=b"ID3ok"))
    r = client.post("/connect/azure-speech",
                    data={"api_key": "good-key", "region": "westeurope"},
                    follow_redirects=False)
    assert "connected=Balss" in r.headers["location"]


def test_reel_build_speaks_the_script(session, monkeypatch, tmp_path):
    """Lēmuma solī ierunas teksts aiziet uz Azure un audio — reelā."""
    from app import pipeline, reels
    from app.models import Article

    credentials.put(session, "azure_speech_key", "test-key")
    article = Article(guid="v-1", url="https://tv3.lv/v", canonical_url="https://tv3.lv/v",
                      title="Kas zināms par namu", section="news",
                      images=["https://cdn/i.jpg"], raw_json={})
    session.add(article)
    session.flush()

    monkeypatch.setattr(reels, "available", lambda: True)
    monkeypatch.setattr(httpx, "post",
                        lambda *a, **k: httpx.Response(200, content=b"ID3mp3"))
    monkeypatch.setattr(tts, "_cache_path",
                        lambda text, voice, out_dir: tmp_path / "voice.mp3")
    built = {}

    def fake_build(title, section, image, points, out_dir=None, voice=None,
                   sections=None, point_images=None, report=None, **kw):
        built.update(voice=voice, **kw)
        if report is not None:
            report.update(voiced=True,
                          narration=[kw.get("cover_voice", "")])
        return "/data/cards/reel_v.mp4"

    monkeypatch.setattr(reels, "build_reel", fake_build)
    fmt, media, recipe = pipeline.resolve_format(
        session, "ig_tv3lv", {"formats": ["reel"], "platform": "instagram"},
        article, {"format": "reel", "card_points": ["Pirmais fakts", "Otrais fakts"],
                  "voice_script": ("Namā daļēji iebruka jumts un lēmums par "
                                   "ēkas nākotni vēl nav pieņemts.")})

    assert fmt == "reel"
    # Vāks runā VIRSRAKSTU. Modeļa rakstītais āķis te vairs neiet: tas bija
    # gan garš, gan saturiski tas pats, ko pirmā nodaļa.
    assert built["voice"] is None
    assert built["cover_voice"] == "Kas zināms par namu."
    assert recipe["voice_script"].startswith("Kas zināms par namu")


def test_reel_stays_silent_when_the_script_is_a_stub(session, monkeypatch):
    from app import pipeline, reels
    from app.models import Article

    credentials.put(session, "azure_speech_key", "test-key")
    article = Article(guid="v-2", url="https://tv3.lv/v2", canonical_url="https://tv3.lv/v2",
                      title="Ziņa", section="news", images=["https://cdn/i.jpg"],
                      raw_json={})
    session.add(article)
    session.flush()

    monkeypatch.setattr(reels, "available", lambda: True)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _never_called())
    built = {}

    def fake_build(title, section, image, points, out_dir=None, voice=None,
                   sections=None, point_images=None, report=None, **kw):
        built.update(voice=voice, **kw)
        return "/data/cards/reel_v2.mp4"

    monkeypatch.setattr(reels, "build_reel", fake_build)
    pipeline.resolve_format(
        session, "ig_tv3lv", {"formats": ["reel"], "platform": "instagram"},
        article, {"format": "reel", "card_points": ["Viens fakts", "Otrs fakts"],
                  "voice_script": "Par īsu."})
    # īss scenārijs vairs nenozīmē klusu lenti: vāks tāpat nolasa virsrakstu,
    # un nodaļu tekstus — balss. Azure te netiek aiztikts, jo sintēze notiek
    # build_reel iekšienē (šeit aizvietots).
    assert built["voice"] is None
    assert built["cover_voice"] == "Ziņa."


def test_a_second_key_is_verified_against_azure_not_the_cache(client, session,
                                                              monkeypatch):
    """Pārbaudei jāaiziet līdz Azure ar jauno atslēgu.

    Ar kešu tas pats parauga teikums atbildētu no iepriekšējās atslēgas
    faila, un nederīga atslēga tiktu apstiprināta kā strādājoša.
    """
    replies = [httpx.Response(200, content=b"ID3ok"),
               httpx.Response(401, text="Unauthorized")]
    seen = []

    def fake_post(*a, **k):
        seen.append(k["headers"]["Ocp-Apim-Subscription-Key"])
        return replies[len(seen) - 1]

    monkeypatch.setattr(httpx, "post", fake_post)
    ok = client.post("/connect/azure-speech",
                     data={"api_key": "good-key", "region": "westeurope"},
                     follow_redirects=False)
    assert "connected=Balss" in ok.headers["location"]

    bad = client.post("/connect/azure-speech",
                      data={"api_key": "revoked-key", "region": "westeurope"},
                      follow_redirects=False)
    assert "/connect?error=" in bad.headers["location"]
    assert seen == ["good-key", "revoked-key"]


def test_unknown_provider_stays_silent_instead_of_crashing(keyed, tmp_path,
                                                           monkeypatch):
    """Nepazīstams tts_provider nedrīkst nogāzt lentes būvēšanu."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _never_called())
    rules = {"reel_voice": True, "tts_provider": "tilde"}
    assert tts.enabled(rules, keyed) is False
    assert tts.synthesize("Teksts, ko neviens nenolasīs.", tmp_path,
                          rules=rules, session=keyed) == ""


def test_voice_catalogue_is_per_provider():
    # "female"/"male" ir mūsu īsvārdi, un tos pārtulko tikai tas pakalpojums,
    # kura katalogs mums ir
    assert tts.voice_name({"reel_voice_name": "male"}) == "lv-LV-NilsNeural"
    # pilns nosaukums iet cauri kā ir — tā var izmēģināt balsi, kas sarakstā
    # vēl nav
    assert tts.voice_name({"reel_voice_name": "lv-LV-EveritaNeural"}) \
        == "lv-LV-EveritaNeural"
    # nezināmam pakalpojumam katalogs ir tukšs, tāpēc netulko neko; kaitēt tas
    # nevar, jo tāds pakalpojums vispār netiek izsaukts (skat. testu augstāk)
    assert tts.voice_name({"tts_provider": "tilde",
                           "reel_voice_name": "Marta"}) == "Marta"


def test_azure_stays_the_default():
    assert tts.provider({}) == "azure"
    assert tts.provider({"tts_provider": " AZURE "}) == "azure"


# --- reģions un diagnostika ------------------------------------------------

def test_region_read_from_whatever_is_pasted():
    assert tts.normalize_region("  WestEurope ") == "westeurope"
    assert tts.normalize_region(
        "https://northeurope.tts.speech.microsoft.com/cognitiveservices/v1") \
        == "northeurope"
    assert tts.normalize_region("westeurope.api.cognitive.microsoft.com") \
        == "westeurope"
    # Foundry adresēs reģiona nav — pirmā etiķete ir RESURSA nosaukums
    assert tts.normalize_region(
        "https://tv3-audio-autopilot-resource.services.ai.azure.com") == ""
    assert tts.normalize_region(
        "https://tv3-audio-autopilot-resource.openai.azure.com/o") == ""
    assert tts.normalize_region("") == ""


def test_azure_error_reaches_the_caller(keyed, tmp_path, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(
        401, text='{"error":"Access denied due to invalid subscription key"}'))
    errors: list[str] = []
    assert tts.synthesize("Teksts.", tmp_path, rules={"reel_voice": True},
                          session=keyed, errors=errors) == ""
    assert errors and errors[0].startswith("HTTP 401")
    assert "invalid subscription key" in errors[0]


def test_pasted_endpoint_is_rejected_with_an_explanation(client, session,
                                                         monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _never_called())
    r = client.post("/connect/azure-speech",
                    data={"api_key": "some-key",
                          "region": "https://tv3-audio-autopilot-resource"
                                    ".services.ai.azure.com"},
                    follow_redirects=False)
    location = r.headers["location"]
    assert "/connect?error=" in location
    assert "Location" in unquote(location)      # pasaka, kur reģionu meklēt
    # atslēga netiek saglabāta ar nederīgu reģionu
    assert credentials.get("azure_speech_key", session) == ""


def test_failed_sample_shows_the_azure_reason(client, session, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(
        403, text="Forbidden: region mismatch"))
    r = client.post("/connect/azure-speech",
                    data={"api_key": "some-key", "region": "westeurope"},
                    follow_redirects=False)
    message = unquote(r.headers["location"])
    assert "HTTP 403" in message and "region mismatch" in message
    assert "westeurope" in message


# --- kā redzēt, vai ieruna strādā ------------------------------------------

def _reel(session, guid, voiced, score, days_ago=1):
    """Publicēts reels ar izmērītu rezultātu."""
    from datetime import timedelta

    from app.models import Article, Post, PostMetrics, utcnow

    a = Article(guid=guid, url=f"https://tv3.lv/{guid}",
                canonical_url=f"https://tv3.lv/{guid}", title="Ziņa",
                section="news", raw_json={})
    session.add(a)
    session.flush()
    p = Post(article_id=a.id, channel="ig_tv3lv", format="reel",
             copy="Teksts", link_url=a.canonical_url, state="published",
             published_at=utcnow() - timedelta(days=days_ago),
             media=["reel.mp4"],
             extra={"recipe": {"kind": "article_reel", "voiced": voiced,
                               "voice_script": "Teksts, ko nolasa balss."}})
    session.add(p)
    session.flush()
    session.add(PostMetrics(post_id=p.id, ga_sessions=score, clicks=score // 2,
                            impressions=score * 10))
    session.flush()
    return p


def test_has_voice_reads_the_build_flag(session):
    from app import reels

    voiced = _reel(session, "hv-1", True, 100)
    silent = _reel(session, "hv-2", False, 50)
    assert reels.has_voice(voiced) is True
    assert reels.has_voice(silent) is False

    # scenārijs receptē BEZ karoga nozīmē, ka sintēze neizdevās — nav balss
    silent.extra = {"recipe": {"voice_script": "Teksts."}}
    assert reels.has_voice(silent) is False


def test_voice_summary_compares_reels_with_each_other(session):
    from app import priors

    for i in range(3):
        _reel(session, f"v-{i}", True, 120)
    for i in range(3):
        _reel(session, f"s-{i}", False, 60)
    session.commit()

    out = priors.voice_summary(session)
    assert out["voiced"]["n"] == 3 and out["silent"]["n"] == 3
    assert out["voiced"]["avg"] == 120 and out["silent"]["avg"] == 60
    assert out["enough"] is True
    assert out["lift"] == pytest.approx(2.0)


def test_voice_summary_ignores_other_formats(session):
    from app.models import Article, Post, utcnow

    from app import priors

    _reel(session, "only-1", True, 100)
    _reel(session, "only-2", False, 100)
    a = Article(guid="lnk", url="https://tv3.lv/l", canonical_url="https://tv3.lv/l",
                title="Saite", section="news", raw_json={})
    session.add(a)
    session.flush()
    session.add(Post(article_id=a.id, channel="fb_tv3lv", format="link",
                     copy="T", state="published", published_at=utcnow()))
    session.commit()

    out = priors.voice_summary(session)
    assert out["voiced"]["n"] + out["silent"]["n"] == 2   # saites posts ārā


def test_voice_summary_withholds_a_verdict_on_thin_data(session):
    from app import priors

    _reel(session, "thin-1", True, 500)
    for i in range(3):
        _reel(session, f"thin-s{i}", False, 10)
    session.commit()

    out = priors.voice_summary(session)
    assert out["enough"] is False      # ar balsi tikai viens ieraksts
    assert out["lift"] is None         # attiecību nerādām


def test_stats_page_shows_the_voice_comparison(client, session):
    for i in range(3):
        _reel(session, f"sp-v{i}", True, 90)
    for i in range(3):
        _reel(session, f"sp-s{i}", False, 30)
    session.commit()

    body = client.get("/stats").text
    assert "Reelu ieruna" in body
    assert "Ar balsi" in body
    assert "+200%" in body            # 90 pret 30


def test_preview_lets_you_hear_the_reel(session, monkeypatch):
    """Ierunātu lenti vajag dzirdēt pirms publicēšanas, nevis pēc."""
    from fastapi.testclient import TestClient

    from app.main import app

    post = _reel(session, "hear-1", True, 0)
    session.commit()
    with TestClient(app) as c:
        c.post("/setup", data={"password": "slepens123", "password2": "slepens123"})
        body = c.get(f"/post/{post.id}/preview").text
    assert "<video" in body
    assert "muted" not in body.split("<video")[1].split(">")[0]


# --- izruna ----------------------------------------------------------------

def test_tv3_domain_is_read_as_three_not_third():
    """Latviski punkts aiz cipara ir kārtas skaitlis: «tv3.lv» balss pati
    nolasīja kā «tv TREŠAIS punkts lv». Domēnā tas ir tikai punkts."""
    assert tts.spoken_text("Vairāk lasi tv3.lv.") == "Vairāk lasi tēvētrīs punkts lv."
    assert "trešais" not in tts.spoken_text("lasi tv3.lv")
    # arī lielie burti un teksts bez domēna
    assert tts.spoken_text("Skaties TV3 Play") == "Skaties tēvētrīs pleij"
    assert tts.spoken_text("TV3 raidījums") == "tēvētrīs raidījums"
    # zīmols ir VIENS vārds: ar atstarpi runātājs tur ietur pauzi
    assert "tv trīs" not in tts.spoken_text("Pilnu stāstu lasi tv3.lv.")


def test_longer_entries_win_over_shorter():
    """«tv3.lv» nedrīkst tikt sadalīts pa «tv3», atstājot «.lv» karājoties."""
    out = tts.spoken_text("tv3.lv")
    assert out == "tēvētrīs punkts lv"
    assert ".lv" not in out


def test_pronunciation_can_be_extended_without_a_deploy():
    rules = {"tts_pronunciation": {"LETA": "leta", "utt.": "un tā tālāk"}}
    out = tts.spoken_text("Ziņu aģentūra LETA, utt.", rules)
    assert "leta" in out and "un tā tālāk" in out
    # noklusējumi paliek spēkā līdzās pielāgotajiem
    assert tts.spoken_text("tv3.lv", rules) == "tēvētrīs punkts lv"


def test_ssml_carries_the_spoken_form():
    doc = tts.build_ssml("Namā iebruka jumts. Lasi tv3.lv.")
    assert "tēvētrīs punkts lv" in doc
    assert "tv3.lv" not in doc


def test_written_script_is_left_alone():
    """Priekšskatījumā redaktors grib redzēt «tv3.lv», nevis fonētiku."""
    from app import reels

    script = reels.voice_script(
        "Namā daļēji iebruka jumts un pagalmā vēl guļ gruveši. Lēmums par "
        "ēkas nākotni joprojām nav pieņemts. Lasi visu tv3.lv.")
    assert "tv3.lv" in script          # rakstiskajā tekstā domēns paliek
    assert "tēvētrīs" in tts.spoken_text(script)   # izrunā tas kļūst par skaņu


def test_cache_key_follows_the_spoken_form(keyed, tmp_path, monkeypatch):
    """Pielabojot izrunas vārdnīcu, vecais ieraksts nedrīkst atbildēt no keša."""
    calls = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: (
        calls.append(k["content"]), httpx.Response(200, content=b"ID3x"))[1])

    text = "Namā iebruka jumts un jaunumi ir šeit: tv3.lv"
    first = tts.synthesize(text, tmp_path, rules={"reel_voice": True},
                           session=keyed)
    # tā pati izruna -> kešs
    tts.synthesize(text, tmp_path, rules={"reel_voice": True}, session=keyed)
    assert len(calls) == 1

    # cita izruna -> jauns fails, jauns pieprasījums
    other = tts.synthesize(text, tmp_path, session=keyed, rules={
        "reel_voice": True,
        "tts_pronunciation": {"tv3.lv": "tevē trīs punkts el vē"}})
    assert other != first and len(calls) == 2


# --- ElevenLabs aiz tās pašas pakalpojumu robežas ---------------------------

def test_elevenlabs_sits_behind_the_same_provider_boundary(session, monkeypatch):
    """Pakalpojumu maina viens noteikums; kešs, teksta sagatavošana un kļūdu
    apstrāde ir kopīga."""
    credentials.put(session, "elevenlabs_api_key", "el-key")
    rules = {"tts_provider": "elevenlabs", "reel_voice": True}
    assert tts.provider(rules) == "elevenlabs"
    assert tts.enabled(rules, session)
    # bez elevenlabs atslēgas elevenlabs nav gatavs, kaut Azure atslēga būtu
    credentials.put(session, "elevenlabs_api_key", "")
    credentials.put(session, "azure_speech_key", "az-key")
    assert not tts.enabled(rules, session)
    assert tts.enabled({"tts_provider": "azure", "reel_voice": True}, session)


def test_elevenlabs_request_carries_key_model_and_spoken_text(session, monkeypatch):
    """Pieprasījumam jāaiziet ar xi-api-key, izvēlēto modeli un jau
    sagatavotu tekstu — izrunas vārdnīca un skaitļi vārdos strādā tāpat kā
    Azure ceļā, jo pārraksta tekstu, ne marķējumu."""
    credentials.put(session, "elevenlabs_api_key", "el-key")
    seen = {}

    def fake_post(url, timeout=None, headers=None, json=None, **kw):
        seen.update(url=url, headers=headers, json=json)
        return httpx.Response(200, content=b"ID3mp3")

    monkeypatch.setattr(httpx, "post", fake_post)
    rules = {"tts_provider": "elevenlabs", "reel_voice_name": "balss-id-123",
             "tts_pronunciation": {"tv3.lv": "tēvētrīs punkts lv"}}
    audio = tts._elevenlabs_audio("Vārti 59. minūtē. Lasi tv3.lv",
                                  tts.voice_name(rules), session=session,
                                  rules=rules)
    assert audio == b"ID3mp3"
    assert "/v1/text-to-speech/balss-id-123" in seen["url"]
    assert seen["headers"]["xi-api-key"] == "el-key"
    assert seen["json"]["model_id"] == "eleven_v3"
    assert "piecdesmit devītajā minūtē" in seen["json"]["text"]
    assert "tēvētrīs punkts lv" in seen["json"]["text"]


def test_elevenlabs_failure_is_reported_not_raised(session, monkeypatch):
    credentials.put(session, "elevenlabs_api_key", "el-key")
    monkeypatch.setattr(httpx, "post",
                        lambda *a, **k: httpx.Response(401, text="bad key"))
    errors: list = []
    out = tts._elevenlabs_audio("Teksts.", "v", session=session, errors=errors,
                                rules={"tts_provider": "elevenlabs"})
    assert out == b"" and errors and "401" in errors[0]


def test_voice_names_resolve_per_provider():
    assert tts.voice_name({"tts_provider": "azure",
                           "reel_voice_name": "female"}) == "lv-LV-EveritaNeural"
    assert tts.voice_name({"tts_provider": "elevenlabs",
                           "reel_voice_name": "female"}) == "21m00Tcm4TlvDq8ikWAM"
    # balss ID iet cauri tāds, kāds ir
    assert tts.voice_name({"tts_provider": "elevenlabs",
                           "reel_voice_name": "xYz123"}) == "xYz123"
    # bez izvēles katrs pakalpojums krīt uz SAVU sievietes balsi, ne Azure
    assert tts.voice_name({"tts_provider": "elevenlabs"}) == "21m00Tcm4TlvDq8ikWAM"


def test_saving_an_elevenlabs_key_verifies_against_elevenlabs(client, session,
                                                              monkeypatch):
    """Pārbaudei jāiet ar elevenlabs arī tad, ja Noteikumos vēl ir azure —
    atslēgu pārbauda tam pakalpojumam, kuram tā pieder."""
    hit = {}

    def fake_post(url, timeout=None, headers=None, json=None, **kw):
        hit.update(url=url, key=(headers or {}).get("xi-api-key"))
        return httpx.Response(200, content=b"ID3mp3")

    monkeypatch.setattr(httpx, "post", fake_post)
    resp = client.post("/connect/elevenlabs", data={"api_key": "el-new"},
                       follow_redirects=False)
    assert resp.status_code == 303 and "error" not in resp.headers["location"]
    assert "api.elevenlabs.io" in hit["url"] and hit["key"] == "el-new"
    assert credentials.get("elevenlabs_api_key", session) == "el-new"


def test_the_catalogue_reports_what_the_account_may_actually_use(session,
                                                                 monkeypatch):
    """Balss ID iekodēt ir minēšana: bezmaksas plānā bibliotēkas balsis caur
    API ir liegtas (402), un kura balss kurā grupā, katram kontam atšķiras."""
    credentials.put(session, "elevenlabs_api_key", "el-key")

    def fake_get(url, headers=None, timeout=None, **kw):
        assert headers["xi-api-key"] == "el-key"
        if "/v2/voices" in url:
            return httpx.Response(200, json={"voices": [
                {"voice_id": "own-1", "name": "Mana balss", "category": "cloned",
                 "labels": {"gender": "Male", "accent": "american",
                            "description": "deep"},
                 "preview_url": "https://cdn/own-1.mp3"},
                {"voice_id": "lib-1", "name": "Rachel", "category": "premade"},
                {"name": "bez id"}]})
        return httpx.Response(200, json=[
            {"model_id": "eleven_v3", "name": "v3",
             "languages": [{"language_id": "lv", "name": "Latvian"}]},
            {"model_id": "eleven_multilingual_v2", "name": "v2",
             "languages": [{"language_id": "en", "name": "English"}]}])

    monkeypatch.setattr(httpx, "get", fake_get)
    cat = tts.elevenlabs_catalogue(session)
    assert [v["id"] for v in cat["voices"]] == ["own-1", "lib-1"]
    # dzimums, akcents un paraugs: bez tiem «kuru vīriešu balsi» nav atbildams
    male = cat["voices"][0]
    assert male["gender"] == "male" and male["accent"] == "american"
    assert male["preview"] == "https://cdn/own-1.mp3"
    # trūkstoši lauki nav kļūda — vienkārši tukši
    assert cat["voices"][1]["gender"] == "" and cat["voices"][1]["preview"] == ""
    assert {m["id"]: m["latvian"] for m in cat["models"]} == {
        "eleven_v3": True, "eleven_multilingual_v2": False}


def test_the_catalogue_is_a_helper_not_a_dependency(session, monkeypatch):
    """Saraksta neizdošanās nedrīkst salauzt Kontu lapu."""
    credentials.put(session, "elevenlabs_api_key", "el-key")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _never_called())
    assert tts.elevenlabs_catalogue(session) == {"voices": [], "models": []}
    # bez atslēgas nav ko prasīt
    credentials.put(session, "elevenlabs_api_key", "")
    assert tts.elevenlabs_catalogue(session) == {}


def test_a_blocked_voice_says_what_to_do_about_it(session, monkeypatch):
    """«HTTP 402» viens pats izskatās pēc koda kļūdas; tas ir plāna jautājums,
    un labojums ir cita balss, ne cita atslēga."""
    credentials.put(session, "elevenlabs_api_key", "el-key")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(
        402, text='{"detail":{"code":"paid_plan_required"}}'))
    errors: list = []
    tts._elevenlabs_audio("Teksts.", "lib-voice", session=session,
                          errors=errors, rules={"tts_provider": "elevenlabs"})
    assert "402" in errors[0] and "reel_voice_name" in errors[0]


def test_choosing_a_voice_writes_the_rule_and_keeps_the_comments(tmp_path,
                                                                 monkeypatch):
    """Ielasīt un izrakstīt YAML nogalinātu komentārus, un tieši tie šajā
    failā redaktoram pasaka, ko katrs noteikums dara."""
    from app import config

    rules = tmp_path / "rules.yaml"
    rules.write_text("# balss izvēle\nreel_voice_name: female\n"
                     "# cits noteikums\ntts_provider: azure\n", encoding="utf-8")
    monkeypatch.setattr(config, "RULES_DIR", tmp_path)
    config.set_rule("reel_voice_name", "JBFqnCBsd6RMkjVDRZzb")
    out = rules.read_text(encoding="utf-8")
    assert 'reel_voice_name: "JBFqnCBsd6RMkjVDRZzb"' in out
    assert "# balss izvēle" in out and "tts_provider: azure" in out

    # atslēga, kuras failā nav, tiek pielikta beigās
    config.set_rule("elevenlabs_model", "eleven_v3")
    assert rules.read_text(encoding="utf-8").rstrip().endswith(
        'elevenlabs_model: "eleven_v3"')


def test_choosing_a_voice_is_verified_before_it_is_accepted(client, session,
                                                            monkeypatch, tmp_path):
    """Bez pārbaudes bezmaksas plāna 402 parādītos tikai pēc pirmā reela."""
    from app import config

    rules = tmp_path / "rules.yaml"
    rules.write_text("reel_voice_name: female\ntts_provider: elevenlabs\n",
                     encoding="utf-8")
    monkeypatch.setattr(config, "RULES_DIR", tmp_path)
    credentials.put(session, "elevenlabs_api_key", "el-key")
    used = {}

    def fake_post(url, timeout=None, headers=None, json=None, **kw):
        used["url"] = url
        return httpx.Response(402, text='{"detail":{"code":"paid_plan_required"}}')

    monkeypatch.setattr(httpx, "post", fake_post)
    resp = client.post("/connect/elevenlabs/voice",
                       data={"voice_id": "lib-voice"}, follow_redirects=False)
    assert resp.status_code == 303
    assert "error" in resp.headers["location"]
    assert "lib-voice" in used["url"]


# --- balss un temps pa sadaļām ---------------------------------------------

def test_a_section_may_have_its_own_voice_and_pace():
    """Izklaides ziņa panes citu balsi un ātrāku tempu nekā stāsts par
    pierobežu. Sadaļas, kas nav uzskaitītas, lieto kopīgo izvēli."""
    rules = {"tts_provider": "azure", "reel_voice_name": "female",
             "reel_voice_rate": -4,
             "reel_voice_by_section": {"entertainment": "male"},
             "reel_voice_rate_by_section": {"entertainment": 8}}
    assert tts.voice_name(rules, "news") == "lv-LV-EveritaNeural"
    assert tts.voice_name(rules, "entertainment") == "lv-LV-NilsNeural"
    assert tts.speech_rate(rules, "news") == -4
    assert tts.speech_rate(rules, "entertainment") == 8
    # bez sadaļas — kopīgā izvēle
    assert tts.voice_name(rules) == "lv-LV-EveritaNeural"


def test_the_rate_is_clamped_and_a_bad_value_falls_back():
    assert tts.speech_rate({"reel_voice_rate": 500}) == 40
    assert tts.speech_rate({"reel_voice_rate": -500}) == -40
    assert tts.speech_rate({"reel_voice_rate": "ātri"}) == tts.DEFAULT_RATE_PERCENT
    assert tts.speech_rate({}) == tts.DEFAULT_RATE_PERCENT


def test_azure_receives_the_rate_as_ssml_prosody(session, monkeypatch):
    credentials.put(session, "azure_speech_key", "az-key")
    seen = {}

    def fake_post(url, timeout=None, headers=None, content=None, **kw):
        seen["ssml"] = content.decode("utf-8")
        return httpx.Response(200, content=b"ID3mp3")

    monkeypatch.setattr(httpx, "post", fake_post)
    tts._azure_audio("Teksts.", "lv-LV-NilsNeural", session=session,
                     rules={"tts_provider": "azure"}, rate=8)
    assert 'rate="+8%"' in seen["ssml"]


def test_elevenlabs_only_sends_speed_when_it_is_changed(session, monkeypatch):
    """Vecāki modeļi voice_settings.speed nepieņem, un nederīgs lauks
    nozīmētu klusu lenti visiem, ne tikai ātrākiem."""
    credentials.put(session, "elevenlabs_api_key", "el-key")
    sent = {}

    def fake_post(url, timeout=None, headers=None, json=None, **kw):
        sent.update(json or {})
        return httpx.Response(200, content=b"ID3mp3")

    monkeypatch.setattr(httpx, "post", fake_post)
    rules = {"tts_provider": "elevenlabs"}
    tts._elevenlabs_audio("T.", "v", session=session, rules=rules, rate=0)
    assert "voice_settings" not in sent

    tts._elevenlabs_audio("T.", "v", session=session, rules=rules, rate=10)
    assert sent["voice_settings"]["speed"] == 1.1
    # ārpus ElevenLabs diapazona netiek izlaists
    tts._elevenlabs_audio("T.", "v", session=session, rules=rules, rate=40)
    assert sent["voice_settings"]["speed"] == 1.2


def test_the_cache_key_includes_the_rate(session, monkeypatch, tmp_path):
    """Bez tempa atslēgā ātrāka izklaides ieruna atbildētu ar veco, lēnāko
    failu, un temps izskatītos pēc neieviesta iestatījuma."""
    credentials.put(session, "azure_speech_key", "az-key")
    calls = []

    def fake_post(url, timeout=None, headers=None, content=None, **kw):
        calls.append(content.decode("utf-8"))
        return httpx.Response(200, content=b"ID3mp3")

    monkeypatch.setattr(httpx, "post", fake_post)
    base = {"tts_provider": "azure", "reel_voice_name": "female",
            "reel_voice_rate": -4,
            "reel_voice_rate_by_section": {"entertainment": 10}}
    slow = tts.synthesize("Viens teikums.", tmp_path, rules=base,
                          session=session, section="news")
    fast = tts.synthesize("Viens teikums.", tmp_path, rules=base,
                          session=session, section="entertainment")
    assert slow and fast and slow != fast
    assert len(calls) == 2 and 'rate="-4%"' in calls[0] and 'rate="+10%"' in calls[1]


def test_the_reel_passes_the_section_to_the_voice(monkeypatch, tmp_path):
    from pathlib import Path

    from app import reels

    seen = []

    def fake_assemble(frames, workdir, out, frame_seconds=2.8, durations=None,
                      voice=None, voices=None, kinds=None):
        Path(out).write_bytes(b"mp4")
        return sum(durations)

    monkeypatch.setattr(reels, "_render_frames",
                        lambda docs, out_dir: [tmp_path / f"f{i}.png"
                                               for i in range(len(docs))])
    monkeypatch.setattr(reels, "_assemble", fake_assemble)
    monkeypatch.setattr(reels, "media_duration", lambda p: 3.0)
    reels.build_reel("T", "entertainment", "", [], out_dir=tmp_path,
                     sections=[{"title": "A", "body": "Teksts."}],
                     cover_voice="Virsraksts.", end_voice="Beigas.",
                     synth=lambda text, **kw: seen.append(kw.get("section"))
                     or "/a.m4a")
    assert set(seen) == {"entertainment"}


# --- Kura balss un temps tiešām tika lietots ---------------------------------

def test_voice_choice_says_where_the_voice_and_the_pace_came_from():
    """Sadaļas balsi no Noteikumu faila nolasīt nevar.

    `reel_voice_by_section` piemērs tur ir komentārs, un izkomentēta rinda
    izskatās gluži kā iestatījums — redaktors maina rindu, kas neko nedara,
    un secina, ka nestrādā rīks. Tāpēc rezultāts ir jāpasaka.
    """
    rules = {"tts_provider": "elevenlabs", "reel_voice_name": "female",
             "reel_voice_rate": -4,
             "reel_voice_by_section": {"entertainment": "balss-id"},
             "reel_voice_rate_by_section": {"entertainment": 12}}

    news = tts.voice_choice(rules, "news")
    assert news["voice"] == tts.VOICES["elevenlabs"]["female"]
    assert news["rate"] == -4
    assert news["voice_by_section"] is False and news["rate_by_section"] is False

    fun = tts.voice_choice(rules, "entertainment")
    assert fun["voice"] == "balss-id" and fun["rate"] == 12
    assert fun["voice_by_section"] is True and fun["rate_by_section"] is True

    # tukšs (vai izkomentēts) saraksts = visas sadaļas lieto kopīgo
    empty = tts.voice_choice({"reel_voice_by_section": None,
                              "reel_voice_rate_by_section": None,
                              "reel_voice_name": "male"}, "entertainment")
    assert empty["voice"] == tts.VOICES["azure"]["male"]
    assert empty["voice_by_section"] is False


# --- Noteikumu pārbaude saglabājot ------------------------------------------

def test_rules_that_are_valid_yaml_but_do_nothing_are_caught():
    """Katrs gadījums te ir tāds, kur fails ir derīgs YAML, kods to pieņem
    un vienkārši nedara neko: balss nemainās, temps nemainās, un iemesls no
    ekrāna nav redzams."""
    from app import config

    # atkāpe pazudusi -> nevis sadaļu saraksts, bet viena vērtība
    assert "atkāpes" in config.validate_editable(
        "rules", "reel_voice_by_section: male")
    # pārrakstīts pakalpojums -> klusas lentes
    assert "tts_provider" in config.validate_editable(
        "rules", "tts_provider: elevenlab")
    # temps vārdiem, nevis procentiem
    assert "veselam skaitlim" in config.validate_editable(
        "rules", "reel_voice_rate: fast")
    # ārpus diapazona -> pakalpojums to tik un tā apgrieztu
    assert "-40..40" in config.validate_editable(
        "rules", "reel_voice_rate_by_section:\n  entertainment: 90")
    # un pats piegādātais fails iet cauri
    text = (config.DEFAULT_RULES_DIR / "rules.yaml").read_text(encoding="utf-8")
    assert config.validate_editable("rules", text) is None


def test_the_commented_examples_can_actually_be_uncommented():
    """Piemērs, kuru nevar ieslēgt, ir sliktāks par nekādu piemēru.

    Agrāk sadaļu saraksti bija rakstīti `key: {}` ar piemēru komentārā zem
    tā. Noņemot `#`, sanāca `key: {}` ar atkāpes bloku aiz tā — YAML kļūda.
    Redaktors piemēram sekoja burtiski un dabūja vai nu neko, vai kļūdu.
    """
    import yaml

    from app import config

    text = (config.DEFAULT_RULES_DIR / "rules.yaml").read_text(encoding="utf-8")
    live = text.replace("#  entertainment", "  entertainment")
    live = live.replace("#  fb_tv3lv", "  fb_tv3lv")
    data = yaml.safe_load(live)            # nedrīkst mest YAMLError
    assert data["reel_voice_by_section"] == {"entertainment": "male"}
    assert data["reel_voice_rate_by_section"] == {"entertainment": 8}
    assert config.validate_editable("rules", live) is None
