"""Google Ads adapteris ar viltotu REST: kampaņu formas pa mērķiem, budžets
mikro vienībās, LV mērķēšana, statusa/budžeta atjauninājumi, insights."""
import json

import pytest

import adapters.google_ads as gmod
from adapters.base import PublishError
from adapters.google_ads import GoogleAdsClient
from app import credentials

CREDS = {"google_ads_customer_id": "123-456-7890", "google_ads_developer_token": "dev",
         "google_ads_client_id": "cid", "google_ads_client_secret": "sec",
         "google_ads_refresh_token": "ref", "google_ads_login_customer_id": ""}


class FakeGoogle:
    def __init__(self):
        self.mutations: list[list[dict]] = []
        self.queries: list[str] = []
        self.search_rows: list[dict] = []
        self.headers: list[dict] = []
        self.token_calls = 0
        self.seq = 0

    def post(self, url, json=None, data=None, headers=None, timeout=None):
        if url == gmod.OAUTH_TOKEN_URL:
            self.token_calls += 1
            assert data["grant_type"] == "refresh_token"
            return _resp({"access_token": "acc"})
        self.headers.append(headers or {})
        if url.endswith(":search"):
            self.queries.append(json["query"])
            return _resp({"results": self.search_rows})
        if url.endswith(":mutate"):
            ops = json["mutateOperations"]
            self.mutations.append(ops)
            out = []
            for op in ops:
                key, body = next(iter(op.items()))
                kind = key.replace("Operation", "")
                created = body.get("create") or body.get("update") or {}
                rn = created.get("resourceName", "")
                if not rn or "/-" in rn:
                    self.seq += 1
                    plural = {"campaignBudget": "campaignBudgets", "campaign": "campaigns",
                              "adGroup": "adGroups", "adGroupAd": "adGroupAds",
                              "asset": "assets", "campaignCriterion": "campaignCriteria",
                              "adGroupCriterion": "adGroupCriteria"}[kind]
                    rn = f"customers/1234567890/{plural}/{self.seq}"
                out.append({f"{kind}Result": {"resourceName": rn}})
            return _resp({"mutateOperationResponses": out})
        raise AssertionError(f"unexpected url {url}")


def _resp(payload, status=200):
    class R:
        status_code = status
        text = json.dumps(payload)
        content = b"x"

        def json(self):
            return payload

    return R()


@pytest.fixture()
def google(monkeypatch):
    g = FakeGoogle()
    monkeypatch.setattr(gmod.httpx, "post", g.post)
    real_get = credentials.get
    monkeypatch.setattr(credentials, "get",
                        lambda key, session=None: (CREDS[key] if key in CREDS
                                                   else real_get(key, session)))
    return g


def _ops(google, kind):
    return [next(iter(op.values())) for ops in google.mutations for op in ops
            if kind in op]


def test_not_configured_without_all_keys(monkeypatch):
    monkeypatch.setattr(credentials, "get", lambda key, session=None: "")
    assert GoogleAdsClient().configured() is False


def test_traffic_launch_is_a_demand_gen_campaign_targeting_latvia(google):
    c = GoogleAdsClient()
    assert c.configured() and c.customer_id == "1234567890"
    made = c.launch("a1 Uzvara", "traffic", "https://tv3.lv/x?utm_source=google_paid",
                    ["Uzvara basketbolā"], ["Lasi tv3.lv"], 1000,
                    images={"landscape": "customers/1/assets/9", "square": "customers/1/assets/10",
                            "portrait": "customers/1/assets/11"},
                    logo="customers/1/assets/12")
    ops = google.mutations[-1]
    budget = ops[0]["campaignBudgetOperation"]["create"]
    assert budget["amountMicros"] == str(1000 * 10_000)          # centi -> mikro
    camp = ops[1]["campaignOperation"]["create"]
    assert camp["advertisingChannelType"] == "DEMAND_GEN"
    assert camp["targetSpend"] == {} and camp["status"] == "PAUSED"
    assert camp["name"].startswith(gmod.CAMPAIGN_PREFIX)
    assert camp["containsEuPoliticalAdvertising"] == "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"
    crits = [op["campaignCriterionOperation"]["create"] for op in ops if "campaignCriterionOperation" in op]
    assert crits[0]["location"]["geoTargetConstant"] == f"geoTargetConstants/{gmod.GEO_LV}"
    assert crits[1]["language"]["languageConstant"] == f"languageConstants/{gmod.LANG_LV}"
    ad = ops[-1]["adGroupAdOperation"]["create"]["ad"]
    assert ad["finalUrls"] == ["https://tv3.lv/x?utm_source=google_paid"]
    dg = ad["demandGenMultiAssetAd"]
    assert dg["portraitMarketingImages"] == [{"asset": "customers/1/assets/11"}]
    assert dg["logoImages"] == [{"asset": "customers/1/assets/12"}]
    assert dg["businessName"] == gmod.BUSINESS_NAME
    assert made["campaign_id"] == made["adset_id"]
    assert made["ad_id"].startswith("customers/1234567890/adGroupAds/")
    # Authorization + developer token katrā pieprasījumā; refresh tikai reizi
    assert google.headers[-1]["developer-token"] == "dev"
    assert google.headers[-1]["Authorization"] == "Bearer acc"
    assert google.token_calls == 1


def test_awareness_launch_is_display_cpm_with_frequency_cap(google):
    c = GoogleAdsClient()
    c.launch("a2 TOP 3", "awareness", "https://tv3.lv/", ["Dienas TOP 3 — tv3.lv"],
             ["Svarīgākais dienā"], 500,
             images={"landscape": "customers/1/assets/1", "square": "customers/1/assets/2"},
             logo="customers/1/assets/3")
    ops = google.mutations[-1]
    camp = ops[1]["campaignOperation"]["create"]
    assert camp["advertisingChannelType"] == "DISPLAY" and camp["targetCpm"] == {}
    assert camp["frequencyCaps"][0]["cap"] == gmod.AWARENESS_DAILY_FREQUENCY
    ad = ops[-1]["adGroupAdOperation"]["create"]["ad"]["responsiveDisplayAd"]
    assert ad["longHeadline"]["text"] == "Dienas TOP 3 — tv3.lv"
    assert ad["marketingImages"] == [{"asset": "customers/1/assets/1"}]


def test_brand_search_launch_has_keywords_and_cpc_ceiling(google):
    c = GoogleAdsClient()
    c.launch("brand", "brand_search", "https://tv3.lv/?utm_source=google_paid",
             ["TV3.lv – ziņas un izklaide"], ["Svarīgākais Latvijā"], 300,
             keywords=["tv3", "tv3 ziņas"])
    ops = google.mutations[-1]
    camp = ops[1]["campaignOperation"]["create"]
    assert camp["advertisingChannelType"] == "SEARCH"
    assert camp["targetSpend"]["cpcBidCeilingMicros"] == str(gmod.BRAND_CPC_CEILING_MICROS)
    assert camp["networkSettings"]["targetContentNetwork"] is False
    kws = [op["adGroupCriterionOperation"]["create"]["keyword"] for op in ops
           if "adGroupCriterionOperation" in op]
    assert kws == [{"text": "tv3", "matchType": "PHRASE"},
                   {"text": "tv3 ziņas", "matchType": "PHRASE"}]
    rsa = ops[-1]["adGroupAdOperation"]["create"]["ad"]["responsiveSearchAd"]
    assert rsa["headlines"][0]["text"] == "TV3.lv – ziņas un izklaide"


def test_text_limits_are_enforced_per_format(google):
    c = GoogleAdsClient()
    long = "x" * 200
    c.launch("a3", "traffic", "https://tv3.lv/", [long], [long], 500,
             images={"landscape": "customers/1/assets/1"}, logo="customers/1/assets/2")
    dg = google.mutations[-1][-1]["adGroupAdOperation"]["create"]["ad"]["demandGenMultiAssetAd"]
    assert len(dg["headlines"][0]["text"]) == 40
    assert len(dg["descriptions"][0]["text"]) == 90


def test_upload_images_returns_asset_names_per_variant(google):
    c = GoogleAdsClient()
    out = c.upload_images({"landscape": b"a", "square": b"", "logo": b"c"}, "a1")
    assert set(out) == {"landscape", "logo"}                  # tukšo izlaiž
    op = google.mutations[-1][0]["assetOperation"]["create"]
    assert op["type"] == "IMAGE" and op["imageAsset"]["data"] == "YQ=="


def test_status_and_budget_updates_address_the_campaign(google):
    c = GoogleAdsClient()
    c.set_status("customers/1234567890/campaigns/55", "ACTIVE")
    upd = google.mutations[-1][0]["campaignOperation"]
    assert upd["update"]["status"] == "ENABLED" and upd["updateMask"] == "status"
    c.set_status("customers/1234567890/adGroupAds/55~1", "PAUSED")
    assert "adGroupAdOperation" in google.mutations[-1][0]

    google.search_rows = [{"campaign": {"campaignBudget": "customers/1234567890/campaignBudgets/7"}}]
    c.set_daily_budget("customers/1234567890/campaigns/55", 1200)
    assert "campaign.resource_name = 'customers/1234567890/campaigns/55'" in google.queries[-1]
    upd = google.mutations[-1][0]["campaignBudgetOperation"]
    assert upd["update"]["amountMicros"] == str(1200 * 10_000)
    assert upd["updateMask"] == "amount_micros"


def test_insights_read_only_our_campaigns_and_convert_micros(google):
    google.search_rows = [{"adGroupAd": {"resourceName": "customers/1/adGroupAds/1~2"},
                           "campaign": {"resourceName": "customers/1/campaigns/1"},
                           "metrics": {"costMicros": "6500000", "impressions": "900",
                                       "clicks": "40"}}]
    rows = GoogleAdsClient().insights()
    assert f"campaign.name LIKE '{gmod.CAMPAIGN_PREFIX}%'" in google.queries[-1]
    assert rows == [{"ad_id": "customers/1/adGroupAds/1~2",
                     "campaign_id": "customers/1/campaigns/1",
                     "spend": 6.5, "impressions": 900, "clicks": 40}]


def test_readiness_reports_account_problems(google):
    google.search_rows = [{"customer": {"descriptiveName": "TV3", "currencyCode": "USD",
                                        "status": "SUSPENDED", "testAccount": True}}]
    ready, issues = GoogleAdsClient().readiness()
    assert not ready
    assert any("SUSPENDED" in i for i in issues)
    assert any("testa konts" in i for i in issues)
    assert any("USD" in i for i in issues)
    google.search_rows = [{"customer": {"descriptiveName": "TV3", "currencyCode": "EUR",
                                        "status": "ENABLED", "testAccount": False}}]
    assert GoogleAdsClient().readiness() == (True, [])


def test_api_errors_become_publish_errors(google, monkeypatch):
    def failing(url, **kw):
        if url == gmod.OAUTH_TOKEN_URL:
            return _resp({"access_token": "acc"})
        return _resp({"error": "nope"}, status=429)

    monkeypatch.setattr(gmod.httpx, "post", failing)
    with pytest.raises(PublishError) as exc:
        GoogleAdsClient().search("SELECT customer.id FROM customer")
    assert exc.value.retryable is True


def test_fit_cuts_at_word_boundary():
    from app.adcreative import fit

    assert fit("Uzvara basketbolā pret Lietuvu", 20) == "Uzvara basketbolā"
    assert fit("īss", 20) == "īss"
    assert len(fit("x" * 50, 20)) == 20
