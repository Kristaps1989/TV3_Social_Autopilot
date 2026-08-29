"""X Ads API client — Phase 4, dormant until an X ads account is connected.

The X Ads API needs a separately APPROVED developer app (apply via
ads.x.com) and signs everything with OAuth 1.0a — the same keys the organic
X adapter already holds, plus the ads account id (x_ads_account_id).
Latvia's X inventory is small, so this stays off until the numbers argue
otherwise; the budget allocator treats it as just another platform when it
does come on.

Campaign structure: funding instrument -> campaign -> line item ->
promoted tweet. Objective WEBSITE_CLICKS mirrors our Meta traffic campaign.
"""
from __future__ import annotations

import logging

import httpx

from adapters.base import PublishError
from adapters.x import oauth1_header
from app import credentials

ADS_API = "https://ads-api.x.com/12"

log = logging.getLogger(__name__)


class XAdsClient:
    def __init__(self, session=None):
        self.account_id = credentials.get("x_ads_account_id", session)
        self.api_key = credentials.get("x_api_key", session)
        self.api_secret = credentials.get("x_api_secret", session)
        self.access_token = credentials.get("x_access_token", session)
        self.access_secret = credentials.get("x_access_secret", session)

    def configured(self) -> bool:
        return all([self.account_id, self.api_key, self.api_secret,
                    self.access_token, self.access_secret])

    def _auth(self, method: str, url: str, params: dict | None = None) -> dict:
        return {"Authorization": oauth1_header(
            method, url, consumer_key=self.api_key,
            consumer_secret=self.api_secret, token=self.access_token,
            token_secret=self.access_secret, extra_params=params)}

    def _request(self, method: str, path: str, params: dict | None = None) -> dict:
        url = f"{ADS_API}/accounts/{self.account_id}/{path}"
        params = {k: str(v) for k, v in (params or {}).items()}
        if method == "GET":
            resp = httpx.get(url, params=params, timeout=30,
                             headers=self._auth("GET", url, params))
        else:
            resp = httpx.post(url, data=params, timeout=60,
                              headers=self._auth("POST", url, params))
        if resp.status_code == 429 or resp.status_code >= 500:
            raise PublishError(f"X Ads {resp.status_code}: {resp.text[:200]}",
                               retryable=True)
        if resp.status_code >= 400:
            raise PublishError(f"X Ads {resp.status_code}: {resp.text[:200]}",
                               retryable=False)
        return resp.json().get("data", {})

    def funding_instruments(self) -> list[dict]:
        out = self._request("GET", "funding_instruments")
        return out if isinstance(out, list) else [out]

    def create_campaign(self, name: str, funding_instrument_id: str,
                        daily_budget_cents: int) -> str:
        return self._request("POST", "campaigns", {
            "name": name, "funding_instrument_id": funding_instrument_id,
            "daily_budget_amount_local_micro": daily_budget_cents * 10_000,
            "entity_status": "PAUSED",
        })["id"]

    def create_line_item(self, campaign_id: str, name: str) -> str:
        return self._request("POST", "line_items", {
            "campaign_id": campaign_id, "name": name,
            "objective": "WEBSITE_CLICKS", "product_type": "PROMOTED_TWEETS",
            "placements": "ALL_ON_TWITTER",
            "entity_status": "PAUSED",
        })["id"]

    def promote_tweet(self, line_item_id: str, tweet_id: str) -> str:
        return self._request("POST", "promoted_tweets", {
            "line_item_id": line_item_id, "tweet_ids": tweet_id,
        })["id"]

    def set_status(self, kind: str, object_id: str, status: str) -> None:
        """kind: campaigns|line_items; status: ACTIVE|PAUSED."""
        self._request("POST", f"{kind}/{object_id}", {"entity_status": status})
