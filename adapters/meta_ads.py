"""Meta Marketing API client for the ads autopilot.

Works against the connected ad account (fb_ad_account_id) with the stored
long-lived USER token — ad accounts belong to the user/Business Manager,
not the page, so the page token cannot manage ads.

Phase 0 uses only the read side (account overview, readiness checks).
The write side (campaigns, ad sets, ads boosting our page posts) is here
and tested, but nothing calls it until ads mode leaves dry-run.
"""
from __future__ import annotations

import json
import logging

import httpx

from adapters.base import PublishError
from app import credentials

GRAPH = "https://graph.facebook.com/v21.0"

log = logging.getLogger(__name__)

# account_status values worth naming for humans (Marketing API docs)
ACCOUNT_STATUS = {1: "aktīvs", 2: "atslēgts", 3: "nenomaksāts rēķins",
                  7: "riska pārskatīšanā", 9: "gaida norēķinu iestatīšanu",
                  100: "slēgšanas procesā", 101: "slēgts"}

REQUIRED_PERMS = ("ads_management", "ads_read")


class MetaAdsClient:
    def __init__(self, session=None):
        self.account_id = credentials.get("fb_ad_account_id", session)
        self.token = credentials.get("fb_user_token", session)
        self.page_id = credentials.get("fb_page_id", session)

    def configured(self) -> bool:
        return bool(self.account_id and self.token)

    # --- plumbing --------------------------------------------------------

    def _act(self) -> str:
        acct = str(self.account_id)
        return acct if acct.startswith("act_") else f"act_{acct}"

    def _get(self, path: str, params: dict | None = None) -> dict:
        r = httpx.get(f"{GRAPH}/{path}", timeout=30,
                      params={**(params or {}), "access_token": self.token})
        if r.status_code >= 400:
            raise PublishError(f"Meta Ads {r.status_code}: {r.text[:200]}",
                               retryable=r.status_code == 429 or r.status_code >= 500)
        return r.json()

    def _post(self, path: str, data: dict) -> dict:
        r = httpx.post(f"{GRAPH}/{path}", timeout=60,
                       data={**data, "access_token": self.token})
        if r.status_code >= 400:
            raise PublishError(f"Meta Ads {r.status_code}: {r.text[:200]}",
                               retryable=r.status_code == 429 or r.status_code >= 500)
        return r.json()

    # --- read side (Phase 0: Konti page + readiness) ---------------------

    def account_overview(self) -> dict:
        d = self._get(self._act(), {"fields": "name,account_status,currency,"
                                              "amount_spent,spend_cap,"
                                              "funding_source_details"})
        status = int(d.get("account_status") or 0)
        return {
            "name": d.get("name", ""),
            "currency": d.get("currency", ""),
            "status": status,
            "status_text": ACCOUNT_STATUS.get(status, str(status)),
            "amount_spent": d.get("amount_spent", ""),
            "spend_cap": d.get("spend_cap", ""),
            "has_funding": bool(d.get("funding_source_details")),
        }

    def readiness(self) -> tuple[bool, list[str]]:
        """(ready, issues) — everything a live campaign will need, checked up
        front so the Konti page can show what is still missing."""
        issues: list[str] = []
        if not self.token:
            issues.append("nav saglabāts lietotāja tokens — pārslēdz Facebook "
                          "savienojumu sadaļā Konti")
        if not self.account_id:
            issues.append("nav izvēlēts reklāmu konts")
        if not self.page_id:
            issues.append("nav savienota Facebook lapa (reklāmas iet tās vārdā)")
        if issues:
            return False, issues
        perms = credentials.fb_token_permissions(self.token)
        if perms:  # [] = pārbaude neizdevās; to nesodām
            for need in REQUIRED_PERMS:
                if need not in perms:
                    issues.append(f"tokenam trūkst {need} atļaujas — pievieno to "
                                  "Login konfigurācijai un pārslēdz Facebook")
        try:
            acc = self.account_overview()
        except PublishError as e:
            issues.append(f"reklāmu konts neatbild: {e}")
            return False, issues
        if acc["status"] != 1:
            issues.append(f"reklāmu konta statuss: {acc['status_text']}")
        if not acc["has_funding"]:
            issues.append("kontam nav maksājumu metodes")
        return not issues, issues

    def insights(self, level: str = "ad", since: str = "", until: str = "") -> list[dict]:
        params = {"level": level,
                  "fields": "ad_id,adset_id,campaign_id,spend,impressions,"
                            "clicks,inline_link_clicks"}
        if since and until:
            params["time_range"] = json.dumps({"since": since, "until": until})
        else:
            params["date_preset"] = "today"
        return self._get(f"{self._act()}/insights", params).get("data", [])

    # --- write side (Phase 1; unused while mode is dry) ------------------

    def create_campaign(self, name: str, objective: str) -> str:
        """Campaign without its own budget — budgets live on ad sets so the
        allocator can steer each ad independently."""
        return self._post(f"{self._act()}/campaigns", {
            "name": name, "objective": objective, "status": "PAUSED",
            "special_ad_categories": "[]",
        })["id"]

    def create_adset(self, campaign_id: str, name: str, daily_budget_cents: int,
                     optimization_goal: str = "LANDING_PAGE_VIEWS") -> str:
        targeting = {"geo_locations": {"countries": ["LV"]},
                     "targeting_automation": {"advantage_audience": 1}}
        return self._post(f"{self._act()}/adsets", {
            "name": name, "campaign_id": campaign_id,
            "daily_budget": str(int(daily_budget_cents)),
            "billing_event": "IMPRESSIONS",
            "optimization_goal": optimization_goal,
            "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
            "targeting": json.dumps(targeting),
            "status": "PAUSED",
        })["id"]

    def create_ad_from_post(self, adset_id: str, name: str,
                            page_post_id: str) -> str:
        """Boost an existing (or dark) page post: the creative is the post
        itself, social proof included."""
        creative = self._post(f"{self._act()}/adcreatives", {
            "name": name,
            "object_story_id": page_post_id,
        })["id"]
        return self._post(f"{self._act()}/ads", {
            "name": name, "adset_id": adset_id,
            "creative": json.dumps({"creative_id": creative}),
            "status": "PAUSED",
        })["id"]

    def set_status(self, object_id: str, status: str) -> None:
        """ACTIVE / PAUSED on a campaign, ad set or ad."""
        self._post(object_id, {"status": status})

    def set_daily_budget(self, adset_id: str, daily_budget_cents: int) -> None:
        self._post(adset_id, {"daily_budget": str(int(daily_budget_cents))})
