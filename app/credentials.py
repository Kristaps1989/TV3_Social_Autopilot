"""Platform credential store + OAuth token exchange helpers.

Lookup order: credentials table (connected via admin UI) -> environment
variable. The DB wins so accounts can be connected/rotated in the browser
without a redeploy; env vars remain for teams that prefer a secret manager.
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta

import httpx

from app.db import get_session
from app.models import Credential, utcnow

log = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com/v21.0"
THREADS_GRAPH = "https://graph.threads.net"

# credential key -> env var fallback
ENV_FALLBACK = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "meta_app_id": "META_APP_ID",
    "meta_app_secret": "META_APP_SECRET",
    "meta_login_config_id": "META_LOGIN_CONFIG_ID",
    "threads_app_id": "THREADS_APP_ID",
    "threads_app_secret": "THREADS_APP_SECRET",
    "fb_page_id": "FB_PAGE_ID",
    "fb_page_token": "FB_PAGE_ACCESS_TOKEN",
    "threads_user_id": "THREADS_USER_ID",
    "threads_token": "THREADS_ACCESS_TOKEN",
    "ig_user_id": "IG_USER_ID",
    "ga4_property_id": "GA4_PROPERTY_ID",
    "fb_ad_account_id": "FB_AD_ACCOUNT_ID",
    "meta_pixel_id": "META_PIXEL_ID",
    "x_api_key": "X_API_KEY",
    "x_api_secret": "X_API_SECRET",
    "x_access_token": "X_ACCESS_TOKEN",
    "x_access_secret": "X_ACCESS_TOKEN_SECRET",
    "x_ads_account_id": "X_ADS_ACCOUNT_ID",
    "google_ads_customer_id": "GOOGLE_ADS_CUSTOMER_ID",
    "google_ads_developer_token": "GOOGLE_ADS_DEVELOPER_TOKEN",
    "google_ads_client_id": "GOOGLE_ADS_CLIENT_ID",
    "google_ads_client_secret": "GOOGLE_ADS_CLIENT_SECRET",
    "google_ads_refresh_token": "GOOGLE_ADS_REFRESH_TOKEN",
    "google_ads_login_customer_id": "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
    "azure_speech_key": "AZURE_SPEECH_KEY",
    "azure_speech_region": "AZURE_SPEECH_REGION",
    "elevenlabs_api_key": "ELEVENLABS_API_KEY",
}


def get(key: str, session=None) -> str:
    own = session is None
    if own:
        session = get_session()
    try:
        row = session.get(Credential, key)
        if row and row.value:
            return row.value
        return os.environ.get(ENV_FALLBACK.get(key, ""), "")
    finally:
        if own:
            session.close()


def put(session, key: str, value: str, label: str = "",
        expires_at: datetime | None = None) -> None:
    row = session.get(Credential, key)
    if row is None:
        row = Credential(key=key)
        session.add(row)
    row.value = value
    row.label = label
    row.expires_at = expires_at
    row.updated_at = utcnow()
    session.commit()


def info(session, key: str) -> Credential | None:
    return session.get(Credential, key)


def new_state(session) -> str:
    """CSRF state for OAuth round-trips."""
    state = secrets.token_urlsafe(24)
    put(session, "oauth_state", state,
        expires_at=utcnow() + timedelta(minutes=15))
    return state


def check_state(session, state: str) -> bool:
    row = session.get(Credential, "oauth_state")
    ok = bool(row and row.value and secrets.compare_digest(row.value, state or "")
              and (row.expires_at is None or row.expires_at > utcnow()))
    if row:
        row.value = ""  # single use
        session.commit()
    return ok


# --- Facebook -------------------------------------------------------------

def fb_app() -> tuple[str, str]:
    """Meta app credentials: entered in the admin UI (DB) or env fallback."""
    return get("meta_app_id"), get("meta_app_secret")


def fb_auth_url(redirect_uri: str, state: str) -> str:
    app_id, _ = fb_app()
    base = (f"https://www.facebook.com/v21.0/dialog/oauth?client_id={app_id}"
            f"&redirect_uri={redirect_uri}&state={state}&response_type=code")
    # "Facebook Login for Business" apps request permissions via a login
    # Configuration (config_id) instead of the classic scope parameter.
    config_id = get("meta_login_config_id")
    if config_id:
        return f"{base}&config_id={config_id}"
    # pages_manage_engagement (pirmais komentārs) prasa līdzi
    # pages_read_user_content — bez pāra Meta dialogs krīt ar "Invalid Scopes".
    # ads_management/ads_read vajag reklāmu autopilotam; Login konfigurācijas
    # lietotājiem tie jāpievieno konfigurācijai Meta pusē.
    scope = ("pages_show_list,pages_manage_posts,pages_read_engagement,"
             "pages_manage_engagement,pages_read_user_content,business_management,"
             "ads_management,ads_read")
    return f"{base}&scope={scope}"


def fb_exchange_code(code: str, redirect_uri: str) -> str:
    """Auth code -> short user token -> long-lived user token."""
    app_id, app_secret = fb_app()
    r = httpx.get(f"{GRAPH}/oauth/access_token", timeout=30, params={
        "client_id": app_id, "client_secret": app_secret,
        "redirect_uri": redirect_uri, "code": code})
    r.raise_for_status()
    short = r.json()["access_token"]
    r = httpx.get(f"{GRAPH}/oauth/access_token", timeout=30, params={
        "grant_type": "fb_exchange_token", "client_id": app_id,
        "client_secret": app_secret, "fb_exchange_token": short})
    r.raise_for_status()
    return r.json()["access_token"]


def fb_extend_user_token(token: str) -> str:
    """Short-lived user token (e.g. from Graph API Explorer) -> long-lived.
    Returns the input unchanged when the exchange is not possible, so page
    tokens can still be derived (they just expire with the user token)."""
    app_id, app_secret = fb_app()
    if not (app_id and app_secret):
        return token
    r = httpx.get(f"{GRAPH}/oauth/access_token", timeout=30, params={
        "grant_type": "fb_exchange_token", "client_id": app_id,
        "client_secret": app_secret, "fb_exchange_token": token})
    if r.status_code != 200:
        log.warning("user token extension failed: %s", r.text[:200])
        return token
    return r.json().get("access_token", token)


def fb_list_pages(user_token: str) -> list[dict]:
    """Pages this user manages; each entry carries its own page token.
    Page tokens derived from a long-lived user token do not expire."""
    r = httpx.get(f"{GRAPH}/me/accounts", timeout=30,
                  params={"access_token": user_token,
                          "fields": "id,name,access_token"})
    r.raise_for_status()
    return r.json().get("data", [])


def fb_list_ad_accounts(user_token: str) -> list[dict]:
    """Ad accounts this user can act on. Ad accounts belong to the user or
    Business Manager, not the page — so this needs the USER token (which the
    connect flow now keeps stored for ads), never the page token."""
    r = httpx.get(f"{GRAPH}/me/adaccounts", timeout=30, params={
        "access_token": user_token,
        "fields": "id,account_id,name,account_status,currency"})
    r.raise_for_status()
    return r.json().get("data", [])


def fb_token_permissions(user_token: str) -> list[str]:
    """Granted permission names on the user token ([] when the call fails —
    callers treat that as 'unknown', not as 'none granted')."""
    r = httpx.get(f"{GRAPH}/me/permissions", timeout=30,
                  params={"access_token": user_token})
    if r.status_code != 200:
        return []
    return [d["permission"] for d in r.json().get("data", [])
            if d.get("status") == "granted"]


def fb_page_instagram(session) -> tuple[str, str]:
    """(ig_user_id, username) of the IG Business account linked to the
    connected Facebook page. Raises with a clear message when absent."""
    page_id, token = get("fb_page_id", session), get("fb_page_token", session)
    if not (page_id and token):
        raise RuntimeError("vispirms jāsavieno Facebook lapa")
    r = httpx.get(f"{GRAPH}/{page_id}", timeout=30, params={
        "fields": "instagram_business_account{id,username}",
        "access_token": token})
    if r.status_code != 200:
        raise RuntimeError(f"Meta atbildēja {r.status_code}: {r.text[:150]}")
    ig = r.json().get("instagram_business_account") or {}
    if not ig.get("id"):
        raise RuntimeError(
            "lapai nav sasaistīta Instagram Business konta, vai lapas tokenam "
            "trūkst instagram_basic atļaujas — pievieno to Login konfigurācijai "
            "un pārslēdz Facebook savienojumu")
    return str(ig["id"]), ig.get("username", "")


# --- Threads --------------------------------------------------------------

def threads_app() -> tuple[str, str]:
    return get("threads_app_id"), get("threads_app_secret")


def threads_auth_url(redirect_uri: str, state: str) -> str:
    app_id, _ = threads_app()
    return (f"https://threads.net/oauth/authorize?client_id={app_id}"
            f"&redirect_uri={redirect_uri}&state={state}"
            f"&scope=threads_basic,threads_content_publish&response_type=code")


def threads_exchange_code(code: str, redirect_uri: str) -> tuple[str, str, datetime]:
    """Auth code -> (user_id, long-lived token, expiry). Long-lived tokens
    last 60 days and must be refreshed (see maintain_tokens)."""
    app_id, app_secret = threads_app()
    r = httpx.post(f"{THREADS_GRAPH}/oauth/access_token", timeout=30, data={
        "client_id": app_id, "client_secret": app_secret,
        "grant_type": "authorization_code", "redirect_uri": redirect_uri,
        "code": code})
    r.raise_for_status()
    data = r.json()
    short, user_id = data["access_token"], str(data["user_id"])
    r = httpx.get(f"{THREADS_GRAPH}/access_token", timeout=30, params={
        "grant_type": "th_exchange_token", "client_secret": app_secret,
        "access_token": short})
    r.raise_for_status()
    data = r.json()
    expires = utcnow() + timedelta(seconds=int(data.get("expires_in", 60 * 86400)))
    return user_id, data["access_token"], expires


def refresh_threads_token(session) -> bool:
    row = info(session, "threads_token")
    if not (row and row.value):
        return False
    r = httpx.get(f"{THREADS_GRAPH}/refresh_access_token", timeout=30, params={
        "grant_type": "th_refresh_token", "access_token": row.value})
    if r.status_code != 200:
        log.warning("threads token refresh failed: %s", r.text[:200])
        return False
    data = r.json()
    put(session, "threads_token", data["access_token"], label=row.label,
        expires_at=utcnow() + timedelta(seconds=int(data.get("expires_in", 60 * 86400))))
    return True


# --- Maintenance ----------------------------------------------------------

def maintain_tokens(session) -> list[str]:
    """Refresh what can be refreshed; return warnings for what expires soon.
    Called daily by the scheduler; warnings also go to the Slack alert."""
    warnings: list[str] = []
    row = info(session, "threads_token")
    if row and row.value and row.expires_at:
        days_left = (row.expires_at - utcnow()).days
        if days_left <= 14:
            if refresh_threads_token(session):
                log.info("threads token refreshed")
            else:
                warnings.append(f"Threads token expires in {days_left}d and refresh failed")
    for key, name in (("fb_page_token", "Facebook"), ("threads_token", "Threads")):
        row = info(session, key)
        if row and row.value and row.expires_at:
            days_left = (row.expires_at - utcnow()).days
            if days_left <= 7:
                warnings.append(f"{name} token expires in {days_left} days — reconnect")
    return warnings


def connection_status(session) -> dict[str, dict]:
    """Per-platform status for the connect page and dashboard."""
    def _status(keys: list[str], label_key: str | None = None) -> dict:
        db_rows = {k: info(session, k) for k in keys}
        connected = all(get(k, session) for k in keys)
        source = ("admin" if any(r and r.value for r in db_rows.values())
                  else "env" if connected else "")
        label = ""
        expires = None
        if label_key and db_rows.get(label_key):
            label = db_rows[label_key].label or ""
            expires = db_rows[label_key].expires_at
        return {"connected": connected, "source": source,
                "label": label, "expires_at": expires}

    return {
        "facebook": _status(["fb_page_id", "fb_page_token"], "fb_page_token"),
        "instagram": _status(["ig_user_id", "fb_page_token"], "ig_user_id"),
        "threads": _status(["threads_user_id", "threads_token"], "threads_token"),
        "x": _status(["x_api_key", "x_api_secret", "x_access_token", "x_access_secret"],
                     "x_access_token"),
        "google_ads": _status(["google_ads_customer_id", "google_ads_developer_token",
                               "google_ads_client_id", "google_ads_client_secret",
                               "google_ads_refresh_token"], "google_ads_customer_id"),
        "meta_ads": _status(["fb_ad_account_id", "fb_user_token"], "fb_ad_account_id"),
    }
