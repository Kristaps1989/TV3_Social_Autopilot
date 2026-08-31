"""Balss sintēze reelu ierunai."""
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
