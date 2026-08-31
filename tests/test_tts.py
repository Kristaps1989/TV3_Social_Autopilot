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

    def fake_build(title, section, image, points, out_dir=None, voice=None):
        built["voice"] = voice
        return "/data/cards/reel_v.mp4"

    monkeypatch.setattr(reels, "build_reel", fake_build)
    fmt, media, recipe = pipeline.resolve_format(
        session, "ig_tv3lv", {"formats": ["reel"], "platform": "instagram"},
        article, {"format": "reel", "card_points": ["Pirmais fakts", "Otrais fakts"],
                  "voice_script": ("Namā daļēji iebruka jumts un lēmums par "
                                   "ēkas nākotni vēl nav pieņemts.")})

    assert fmt == "reel"
    assert built["voice"] == str(tmp_path / "voice.mp3")
    assert recipe["voice_script"].startswith("Namā daļēji iebruka jumts")


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

    def fake_build(title, section, image, points, out_dir=None, voice=None):
        built["voice"] = voice
        return "/data/cards/reel_v2.mp4"

    monkeypatch.setattr(reels, "build_reel", fake_build)
    pipeline.resolve_format(
        session, "ig_tv3lv", {"formats": ["reel"], "platform": "instagram"},
        article, {"format": "reel", "card_points": ["Viens fakts", "Otrs fakts"],
                  "voice_script": "Par īsu."})
    assert built["voice"] is None


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
