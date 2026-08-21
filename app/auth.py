"""Session-cookie authentication for the admin UI.

First visit with no password configured -> /setup creates one (stored as a
scrypt hash in the credentials table). After that every page requires login
at /login; a signed, expiring cookie keeps the session. The ADMIN_PASSWORD
env var still works as a valid password for teams that prefer env config.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

from app import credentials

SESSION_COOKIE = "tv3ap_session"
SESSION_DAYS = 30
MIN_PASSWORD_LEN = 8


def _secret(session) -> str:
    """Cookie-signing secret; generated once and persisted in the DB."""
    s = credentials.get("session_secret", session)
    if not s:
        s = secrets.token_hex(32)
        credentials.put(session, "session_secret", s)
    return s


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return salt.hex() + "$" + digest.hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                                n=2**14, r=8, p=1)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:  # noqa: BLE001
        return False


def password_configured(session) -> bool:
    return bool(credentials.get("admin_password_hash", session)
                or os.environ.get("ADMIN_PASSWORD"))


def check_password(session, password: str) -> bool:
    stored = credentials.get("admin_password_hash", session)
    if stored and verify_password(password, stored):
        return True
    env_pw = os.environ.get("ADMIN_PASSWORD", "")
    return bool(env_pw) and hmac.compare_digest(password, env_pw)


def set_password(session, password: str) -> str | None:
    """Store a new admin password. Returns an error message or None."""
    if len(password) < MIN_PASSWORD_LEN:
        return f"Parolei jābūt vismaz {MIN_PASSWORD_LEN} rakstzīmes garai"
    credentials.put(session, "admin_password_hash", hash_password(password))
    return None


def issue_token(session) -> str:
    expires = int(time.time()) + SESSION_DAYS * 86400
    payload = f"admin.{expires}"
    sig = hmac.new(_secret(session).encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def valid_token(session, token: str) -> bool:
    parts = (token or "").split(".")
    if len(parts) != 3 or parts[0] != "admin":
        return False
    payload = f"{parts[0]}.{parts[1]}"
    sig = hmac.new(_secret(session).encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, parts[2]):
        return False
    try:
        return int(parts[1]) > time.time()
    except ValueError:
        return False
