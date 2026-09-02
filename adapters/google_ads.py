"""Google Ads API klients reklāmu autopilotam (REST, bez smagās bibliotēkas).

Trīs kampaņu veidi, katrs savai lomai tv3.lv stratēģijā:

* traffic      — Demand Gen (Discover, YouTube, Gmail plūsmas), «Maximize
                 clicks»: maksas Discover rakstiem, kas jau organiski iet;
* awareness    — Display ar mērķa CPM un biežuma griestiem: zīmola
                 franšīzes (Dienas TOP 3, Nedēļas TOP 5, Nedēļa 30 sekundēs)
                 plašai auditorijai, kur mērs ir sasniedzamība, ne klikšķi;
* brand_search — Search ar zīmola atslēgvārdiem («tv3», «tv3 ziņas»,
                 «tv3 play»), vienmēr ieslēgts, ar CPC griestiem: aizsargā
                 zīmola vaicājumus no konkurentiem par centiem.

Google budžets dzīvo kampaņā, ne reklāmu grupā, tāpēc viens autopilota
ieraksts = viena kampaņa (budžets + kritēriji + grupa + reklāma). Pārējais
kods redz to pašu saskarni, ko Meta klientam: `set_status`,
`set_daily_budget`, `insights` — identifikatori ir pilni resursu vārdi.

Piekļuve: developer token (Google Ads API Center), OAuth2 klients ar
refresh tokenu, klienta konta ID; pārvaldnieka konts (login-customer-id)
pēc izvēles. Kamēr tā nav, `configured()` ir False un nekas netiek sūtīts.
"""
from __future__ import annotations

import base64
import logging
import re

import httpx

from adapters.base import PublishError
from app import credentials

log = logging.getLogger(__name__)

API_VERSION = "v21"
API = f"https://googleads.googleapis.com/{API_VERSION}"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"

CAMPAIGN_PREFIX = "TV3 Autopilots"
BUSINESS_NAME = "TV3.lv"
GEO_LV = 2428        # geoTargetConstants: Latvija
LANG_LV = 1028       # languageConstants: latviešu
BRAND_CPC_CEILING_MICROS = 400_000   # 0,40 € par zīmola klikšķi ir griesti
AWARENESS_DAILY_FREQUENCY = 3        # cik reizes dienā vienam cilvēkam

# Google teksta limiti (rakstzīmes)
LIMITS = {
    "demand_gen": {"headline": 40, "description": 90},
    "display": {"headline": 30, "long_headline": 90, "description": 90},
    "search": {"headline": 30, "description": 90},
}

STATUS = {"ACTIVE": "ENABLED", "ENABLED": "ENABLED", "PAUSED": "PAUSED"}


def _digits(value: str) -> str:
    return re.sub(r"\D", "", str(value or ""))


def resource_id(resource_name: str) -> str:
    """customers/1/campaigns/42 -> 42 (pēdējais posms)."""
    return str(resource_name or "").rsplit("/", 1)[-1]


class GoogleAdsClient:
    platform = "google_ads"

    def __init__(self, session=None):
        g = lambda k: credentials.get(k, session)  # noqa: E731
        self.customer_id = _digits(g("google_ads_customer_id"))
        self.developer_token = g("google_ads_developer_token")
        self.client_id = g("google_ads_client_id")
        self.client_secret = g("google_ads_client_secret")
        self.refresh_token = g("google_ads_refresh_token")
        self.login_customer_id = _digits(g("google_ads_login_customer_id"))
        self._access_token = ""

    def configured(self) -> bool:
        return all([self.customer_id, self.developer_token, self.client_id,
                    self.client_secret, self.refresh_token])

    # --- plumbing --------------------------------------------------------

    def _token(self) -> str:
        if self._access_token:
            return self._access_token
        r = httpx.post(OAUTH_TOKEN_URL, timeout=30, data={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "refresh_token": self.refresh_token, "grant_type": "refresh_token"})
        if r.status_code >= 400:
            raise PublishError(f"Google OAuth {r.status_code}: {r.text[:200]}",
                               retryable=r.status_code >= 500)
        self._access_token = r.json().get("access_token", "")
        if not self._access_token:
            raise PublishError("Google OAuth neatdeva access token", retryable=False)
        return self._access_token

    def _headers(self) -> dict:
        h = {"Authorization": f"Bearer {self._token()}",
             "developer-token": self.developer_token}
        if self.login_customer_id:
            h["login-customer-id"] = self.login_customer_id
        return h

    def _call(self, method: str, payload: dict) -> dict:
        url = f"{API}/customers/{self.customer_id}/googleAds:{method}"
        r = httpx.post(url, json=payload, headers=self._headers(), timeout=60)
        if r.status_code >= 400:
            raise PublishError(f"Google Ads {r.status_code}: {r.text[:300]}",
                               retryable=r.status_code == 429 or r.status_code >= 500)
        return r.json() if r.content else {}

    def search(self, query: str) -> list[dict]:
        return self._call("search", {"query": query, "pageSize": 1000}).get("results", [])

    def mutate(self, operations: list[dict]) -> list[str]:
        """Viens pieprasījums, visas operācijas kopā (atomiski). Atgriež
        izveidoto/mainīto resursu vārdus operāciju secībā."""
        resp = self._call("mutate", {"mutateOperations": operations,
                                     "partialFailure": False})
        out = []
        for item in resp.get("mutateOperationResponses", []):
            result = next(iter(item.values()), {}) if item else {}
            out.append(str(result.get("resourceName", "")))
        return out

    def _rn(self, kind: str, temp: int) -> str:
        return f"customers/{self.customer_id}/{kind}/{temp}"

    # --- read side ---------------------------------------------------------

    def account_overview(self) -> dict:
        rows = self.search("SELECT customer.id, customer.descriptive_name, "
                           "customer.currency_code, customer.status, "
                           "customer.test_account FROM customer LIMIT 1")
        c = (rows[0] if rows else {}).get("customer", {})
        return {"name": c.get("descriptiveName", ""), "currency": c.get("currencyCode", ""),
                "status": c.get("status", ""), "test_account": bool(c.get("testAccount"))}

    def readiness(self) -> tuple[bool, list[str]]:
        issues: list[str] = []
        for key, label in (("customer_id", "klienta konta ID"),
                           ("developer_token", "developer token"),
                           ("client_id", "OAuth client ID"),
                           ("client_secret", "OAuth client secret"),
                           ("refresh_token", "OAuth refresh token")):
            if not getattr(self, key):
                issues.append(f"trūkst {label} (Konti → Google reklāmas)")
        if issues:
            return False, issues
        try:
            acc = self.account_overview()
        except PublishError as e:
            return False, [f"Google Ads konts neatbild: {e}"]
        if acc["status"] != "ENABLED":
            issues.append(f"konta statuss: {acc['status'] or 'nezināms'}")
        if acc["test_account"]:
            issues.append("tas ir testa konts — reklāmas neizies ēterā")
        if acc["currency"] and acc["currency"] != "EUR":
            issues.append(f"konta valūta {acc['currency']} — budžeti šeit ir eiro")
        return not issues, issues

    def insights(self, level: str = "ad", since: str = "", until: str = "") -> list[dict]:
        """Šodienas tēriņš/klikšķi pa reklāmām mūsu kampaņās; `ad_id` ir
        adGroupAd resursa vārds — tas pats, ko glabā AdEntry.ad_id."""
        period = (f"segments.date BETWEEN '{since}' AND '{until}'"
                  if since and until else "segments.date DURING TODAY")
        rows = self.search(
            "SELECT ad_group_ad.resource_name, campaign.resource_name, "
            "metrics.cost_micros, metrics.impressions, metrics.clicks "
            f"FROM ad_group_ad WHERE campaign.name LIKE '{CAMPAIGN_PREFIX}%' "
            f"AND {period}")
        out = []
        for r in rows:
            m = r.get("metrics", {})
            out.append({"ad_id": r.get("adGroupAd", {}).get("resourceName", ""),
                        "campaign_id": r.get("campaign", {}).get("resourceName", ""),
                        "spend": int(m.get("costMicros") or 0) / 1_000_000,
                        "impressions": int(m.get("impressions") or 0),
                        "clicks": int(m.get("clicks") or 0)})
        return out

    # --- write side --------------------------------------------------------

    def upload_images(self, images: dict[str, bytes], name: str) -> dict[str, str]:
        """{variants -> attēla resursa vārds}. Tukšus izlaiž."""
        keys = [k for k, data in images.items() if data]
        if not keys:
            return {}
        ops = [{"assetOperation": {"create": {
            "name": f"{name} · {k}"[:128], "type": "IMAGE",
            "imageAsset": {"data": base64.b64encode(images[k]).decode()}}}}
            for k in keys]
        names = self.mutate(ops)
        return {k: rn for k, rn in zip(keys, names) if rn}

    def launch(self, name: str, objective: str, link: str, headlines: list[str],
               descriptions: list[str], daily_budget_cents: int,
               images: dict[str, str] | None = None, logo: str = "",
               keywords: list[str] | None = None) -> dict:
        """Kampaņa + budžets + LV mērķēšana + grupa + reklāma vienā mutate.

        Atgriež {campaign_id, adset_id, ad_id} — adset_id ir tā pati
        kampaņa, jo Google budžets un pauze dzīvo tur."""
        name = f"{CAMPAIGN_PREFIX} · {name}"[:120]
        images = images or {}
        budget_rn, camp_rn, group_rn = (self._rn("campaignBudgets", -1),
                                        self._rn("campaigns", -2),
                                        self._rn("adGroups", -3))
        campaign: dict = {
            "resourceName": camp_rn, "name": name, "status": "PAUSED",
            "campaignBudget": budget_rn,
            # ES politiskās reklāmas Google vairs nerāda; deklarācija ir obligāta
            "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
        }
        group: dict = {"resourceName": group_rn, "name": name, "campaign": camp_rn,
                       "status": "ENABLED"}
        ad: dict = {"finalUrls": [link]}
        if objective == "awareness":
            lim = LIMITS["display"]
            campaign.update({"advertisingChannelType": "DISPLAY", "targetCpm": {},
                             "frequencyCaps": [{"key": {"level": "CAMPAIGN",
                                                        "eventType": "IMPRESSION",
                                                        "timeUnit": "DAY",
                                                        "timeLength": 1},
                                                "cap": AWARENESS_DAILY_FREQUENCY}]})
            group["type"] = "DISPLAY_STANDARD"
            ad["responsiveDisplayAd"] = {
                "headlines": [{"text": h[:lim["headline"]]} for h in headlines[:5]],
                "longHeadline": {"text": (headlines[0] if headlines else BUSINESS_NAME)[:lim["long_headline"]]},
                "descriptions": [{"text": d[:lim["description"]]} for d in descriptions[:5]],
                "businessName": BUSINESS_NAME,
                "marketingImages": [{"asset": images[k]} for k in ("landscape",) if k in images],
                "squareMarketingImages": [{"asset": images[k]} for k in ("square",) if k in images],
                "logoImages": [{"asset": logo}] if logo else [],
            }
        elif objective == "brand_search":
            lim = LIMITS["search"]
            campaign.update({"advertisingChannelType": "SEARCH",
                             "targetSpend": {"cpcBidCeilingMicros": str(BRAND_CPC_CEILING_MICROS)},
                             "networkSettings": {"targetGoogleSearch": True,
                                                 "targetSearchNetwork": False,
                                                 "targetContentNetwork": False,
                                                 "targetPartnerSearchNetwork": False}})
            group["type"] = "SEARCH_STANDARD"
            ad["responsiveSearchAd"] = {
                "headlines": [{"text": h[:lim["headline"]]} for h in headlines[:15]],
                "descriptions": [{"text": d[:lim["description"]]} for d in descriptions[:4]],
            }
        else:  # traffic — Demand Gen
            lim = LIMITS["demand_gen"]
            campaign.update({"advertisingChannelType": "DEMAND_GEN", "targetSpend": {}})
            ad["demandGenMultiAssetAd"] = {
                "headlines": [{"text": h[:lim["headline"]]} for h in headlines[:5]],
                "descriptions": [{"text": d[:lim["description"]]} for d in descriptions[:5]],
                "businessName": BUSINESS_NAME,
                "marketingImages": [{"asset": images[k]} for k in ("landscape",) if k in images],
                "squareMarketingImages": [{"asset": images[k]} for k in ("square",) if k in images],
                "portraitMarketingImages": [{"asset": images[k]} for k in ("portrait",) if k in images],
                "logoImages": [{"asset": logo}] if logo else [],
            }
        ops: list[dict] = [
            {"campaignBudgetOperation": {"create": {
                "resourceName": budget_rn, "name": f"{name} · budžets"[:120],
                "amountMicros": str(int(daily_budget_cents) * 10_000),
                "deliveryMethod": "STANDARD", "explicitlyShared": False}}},
            {"campaignOperation": {"create": campaign}},
            {"campaignCriterionOperation": {"create": {
                "campaign": camp_rn,
                "location": {"geoTargetConstant": f"geoTargetConstants/{GEO_LV}"}}}},
            {"campaignCriterionOperation": {"create": {
                "campaign": camp_rn,
                "language": {"languageConstant": f"languageConstants/{LANG_LV}"}}}},
            {"adGroupOperation": {"create": group}},
        ]
        for kw in (keywords or []):
            ops.append({"adGroupCriterionOperation": {"create": {
                "adGroup": group_rn, "status": "ENABLED",
                "keyword": {"text": kw, "matchType": "PHRASE"}}}})
        ops.append({"adGroupAdOperation": {"create": {
            "adGroup": group_rn, "status": "ENABLED", "ad": ad}}})
        names = self.mutate(ops)
        if len(names) != len(ops):
            raise PublishError("Google Ads mutate atdeva negaidītu atbildi", retryable=False)
        return {"campaign_id": names[1], "adset_id": names[1], "ad_id": names[-1],
                "budget_id": names[0]}

    def set_status(self, object_id: str, status: str) -> None:
        """ACTIVE/PAUSED uz kampaņu, grupu vai reklāmu (pēc resursa vārda)."""
        kind = str(object_id).split("/")[-2] if "/" in str(object_id) else "campaigns"
        op = {"campaigns": "campaignOperation", "adGroups": "adGroupOperation",
              "adGroupAds": "adGroupAdOperation"}.get(kind)
        if op is None:
            raise PublishError(f"nezināms Google resurss: {object_id}", retryable=False)
        self.mutate([{op: {"update": {"resourceName": object_id,
                                      "status": STATUS.get(status.upper(), status)},
                           "updateMask": "status"}}])

    def set_daily_budget(self, adset_id: str, daily_budget_cents: int) -> None:
        rows = self.search("SELECT campaign.campaign_budget FROM campaign "
                           f"WHERE campaign.resource_name = '{adset_id}'")
        budget_rn = (rows[0] if rows else {}).get("campaign", {}).get("campaignBudget", "")
        if not budget_rn:
            raise PublishError(f"kampaņai {adset_id} nav budžeta", retryable=False)
        self.mutate([{"campaignBudgetOperation": {
            "update": {"resourceName": budget_rn,
                       "amountMicros": str(int(daily_budget_cents) * 10_000)},
            "updateMask": "amount_micros"}}])
