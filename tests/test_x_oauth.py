"""X pieslēgums ar vienu pogu (OAuth 2.0 + PKCE).

Agrāk X prasīja ielīmēt ČETRAS vērtības no izstrādātāju portāla, kamēr
Facebook un Threads pieslēdzās ar vienu klikšķi. Šie testi iet visu ceļu:
lietotnes dati -> autorizācija -> kods -> marķieri -> publicēšana ar Bearer,
un pārbauda, ka vecais ceļš joprojām strādā tiem, kam tas jau ir.
"""
import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app import credentials
from app.models import utcnow


class FakeX:
    """Minimāls X: marķiera galapunkts, users/me un tvīts."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.refreshes = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append((request.method, url))
        if url.startswith(credentials.X_TOKEN):
            body = parse_qs(request.content.decode())
            if body.get("grant_type") == ["refresh_token"]:
                self.refreshes += 1
                return httpx.Response(200, json={
                    "access_token": "tok-svaigs", "refresh_token": "ref-2",
                    "expires_in": 7200})
            assert body["code_verifier"][0], "PKCE pārbaudītājs jāsūta līdzi"
            return httpx.Response(200, json={
                "access_token": "tok-abc", "refresh_token": "ref-1",
                "expires_in": 7200})
        if url.startswith(credentials.X_ME):
            assert request.headers["Authorization"].startswith("Bearer ")
            return httpx.Response(200, json={"data": {"username": "TV3Zinas"}})
        if "2/media/upload" in url:
            return httpx.Response(200, json={"data": {"id": "media-1"}})
        if url.endswith("/2/tweets"):
            assert request.headers["Authorization"].startswith("Bearer ")
            return httpx.Response(201, json={"data": {"id": "tweet-1"}})
        return httpx.Response(404, json={"detail": url})


@pytest.fixture()
def fake_x(monkeypatch):
    fake = FakeX()
    transport = httpx.MockTransport(fake.handler)
    real_post, real_get = httpx.post, httpx.get

    def post(url, **kw):
        kw.pop("timeout", None)
        with httpx.Client(transport=transport) as c:
            return c.post(url, **kw)

    def get(url, **kw):
        kw.pop("timeout", None)
        with httpx.Client(transport=transport) as c:
            return c.get(url, **kw)

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", get)
    yield fake
    httpx.post, httpx.get = real_post, real_get


def test_authorize_url_carries_pkce_and_both_write_scopes(session):
    credentials.put(session, "x_client_id", "klients")
    url = credentials.x_auth_url(session, "https://app.example/connect/x/callback", "st8")
    q = parse_qs(urlparse(url).query)
    assert url.startswith(credentials.X_AUTHORIZE)
    assert q["client_id"] == ["klients"] and q["state"] == ["st8"]
    assert q["code_challenge_method"] == ["S256"]
    # tvīta un attēla tiesības ir ATSEVIŠĶAS — bez media.write attēls dod 403
    assert "tweet.write" in q["scope"][0] and "media.write" in q["scope"][0]
    assert "offline.access" in q["scope"][0]   # citādi marķieris beidzas pēc 2 h
    # izaicinājums tiešām atbilst saglabātajam pārbaudītājam
    verifier = credentials.info(session, "x_pkce_verifier").value
    expect = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert q["code_challenge"] == [expect]


def test_code_exchange_stores_tokens_and_the_handle(session, fake_x):
    credentials.put(session, "x_client_id", "klients")
    credentials.x_auth_url(session, "https://app.example/cb", "st8")
    got = credentials.x_exchange_code(session, "kods", "https://app.example/cb")
    assert got["token"] == "tok-abc" and got["refresh"] == "ref-1"
    assert credentials.x_handle(got["token"]) == "@TV3Zinas"
    # pārbaudītājs ir vienreizējs
    assert not credentials.info(session, "x_pkce_verifier").value


def test_expiring_token_refreshes_itself_before_publishing(session, fake_x):
    credentials.put(session, "x_client_id", "klients")
    credentials.put(session, "x_oauth_token", "vecs", label="@TV3Zinas",
                    expires_at=utcnow())          # tieši beidzies
    credentials.put(session, "x_oauth_refresh", "ref-1")
    assert credentials.x_access_token(session) == "tok-svaigs"
    assert fake_x.refreshes == 1
    # rotētais atsvaidzinātājs arī tiek saglabāts, citādi nākamā reize kristu
    assert credentials.info(session, "x_oauth_refresh").value == "ref-2"


def test_adapter_publishes_with_bearer_and_v2_media(session, fake_x, monkeypatch):
    credentials.put(session, "x_oauth_token", "tok-abc",
                    expires_at=utcnow().replace(year=2030))
    from adapters.x import XAdapter

    adapter = XAdapter()
    assert adapter.configured() and adapter.oauth2
    monkeypatch.setattr(XAdapter, "_read", staticmethod(lambda m: b"bytes"))
    tweet_id = adapter.publish(text="Sveiki", link="", images=["/tmp/a.png"],
                               fmt="photo", alt_text="apraksts")
    assert tweet_id == "tweet-1"
    # attēls gāja uz v2, ne uz v1.1 — Bearer marķieri v1.1 nepieņem
    assert any("api.x.com/2/media/upload" in u for _, u in fake_x.calls)
    assert not any("upload.twitter.com" in u for _, u in fake_x.calls)


def test_the_old_four_key_setup_still_works(session, monkeypatch):
    """Kam vecais ceļš strādā, tam jāturpina strādāt bez pieskaršanās."""
    for key, value in (("x_api_key", "k"), ("x_api_secret", "s"),
                       ("x_access_token", "t"), ("x_access_secret", "ts")):
        credentials.put(session, key, value)
    from adapters.x import XAdapter

    adapter = XAdapter()
    assert adapter.configured() and not adapter.oauth2
    head = adapter._auth("POST", "https://api.twitter.com/2/tweets")
    assert head["Authorization"].startswith("OAuth ")


def test_the_whole_button_flow_through_the_admin_pages(session, fake_x):
    """No gala līdz galam pa to pašu ceļu, ko iet redaktors: saglabā lietotni,
    spiež pogu, X atsūta atpakaļ ar kodu, konts ir pieslēgts."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    client.post("/setup", data={"password": "slepens123", "password2": "slepens123"})

    # 1. bez lietotnes datiem poga godīgi pasaka, kā trūkst
    r = client.get("/connect/x/start", follow_redirects=False)
    assert r.status_code == 303 and "Client+ID" in r.headers["location"].replace("%20", "+")

    # 2. lietotnes dati (vienreizējs solis, tāpat kā Meta un Threads)
    client.post("/connect/x/app", data={"client_id": "klients", "client_secret": ""},
                follow_redirects=False)

    # 3. poga aizsūta uz X ar PKCE izaicinājumu
    r = client.get("/connect/x/start", follow_redirects=False)
    target = r.headers["location"]
    assert target.startswith(credentials.X_AUTHORIZE)
    q = parse_qs(urlparse(target).query)
    state = q["state"][0]

    # 4. X atsūta atpakaļ ar kodu
    r = client.get(f"/connect/x/callback?code=kods&state={state}",
                   follow_redirects=False)
    assert r.status_code == 303 and "error" not in r.headers["location"]
    assert credentials.get("x_oauth_token") == "tok-abc"
    assert credentials.info(session, "x_oauth_token").label == "@TV3Zinas"

    # 5. Konti lapa rāda pieslēgto kontu, ne četrus tukšus laukus
    page = client.get("/connect").text
    assert "@TV3Zinas" in page and "Pieslēgties ar X kontu" not in page

    # 6. atvienošana notīra abus ceļus
    client.post("/connect/x/disconnect", follow_redirects=False)
    assert credentials.get("x_oauth_token") == ""


def test_a_forged_callback_is_refused(session, fake_x):
    """state ir vienreizējs un pārbaudīts — svešs kods kontu nepieslēdz."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    client.post("/setup", data={"password": "slepens123", "password2": "slepens123"})
    client.post("/connect/x/app", data={"client_id": "klients"},
                follow_redirects=False)
    r = client.get("/connect/x/callback?code=kods&state=svesa", follow_redirects=False)
    assert "error" in r.headers["location"]
    assert credentials.get("x_oauth_token") == ""


def test_idle_days_do_not_kill_the_connection(session, fake_x):
    """Marķieris dzīvo divas stundas. Klusā dienā to neatjauno publicēšana,
    un atsvaidzinātājs var nostāvēt tik ilgi, ka X to atsauc."""
    credentials.put(session, "x_client_id", "klients")
    credentials.put(session, "x_oauth_token", "tok-vecs",
                    expires_at=utcnow().replace(year=2030))
    credentials.put(session, "x_oauth_refresh", "ref-1")
    assert credentials.maintain_tokens(session) == []
    assert fake_x.refreshes == 1
    assert credentials.get("x_oauth_token", session) == "tok-svaigs"


def test_a_connection_without_offline_access_is_reported_not_hidden(session, fake_x):
    """Bez offline.access marķieris beidzas pēc divām stundām un neatgriežas.
    Tas nedrīkst izpausties kā nejauša 401 pēc pusdienlaika."""
    credentials.put(session, "x_oauth_token", "tok-abc",
                    expires_at=utcnow().replace(year=2030))
    credentials.put(session, "x_oauth_refresh", "")
    warnings = credentials.maintain_tokens(session)
    assert any("offline.access" in w for w in warnings)
