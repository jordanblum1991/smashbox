"""TikTok Shop API — auth/token plumbing + request signing.

Scope of this module:
  - exchange_auth_code()  : authorize-code → access/refresh tokens (no signing)
  - refresh_access_token(): rotate the access token before it expires
  - get_authorized_shops(): discover the shop_cipher (a SIGNED call)
  - store_credential() / get_credential() / ensure_fresh_token()
  - signed_params()       : HMAC-SHA256 signing used by every data API call

Endpoints + the exact signing recipe are TikTok-Shop-version-specific and are
verified against real responses during the first live authorization — kept as
module constants so they're a one-line change. Tokens are secrets: never log or
serialize them.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.import_batch import _utc_now_naive
from app.models.tiktok_credential import TikTokCredential

AUTH_BASE = "https://auth.tiktok-shops.com"          # token get/refresh
OPEN_API_BASE = "https://open-api.tiktokglobalshop.com"  # signed data calls
SHOPS_PATH = "/authorization/202309/shops"


# ---- token exchange / refresh (these are NOT signed) -----------------------

def exchange_auth_code(auth_code: str) -> dict:
    """Trade a fresh authorize code for tokens. Returns the `data` payload."""
    import httpx

    r = httpx.get(f"{AUTH_BASE}/api/v2/token/get", params={
        "app_key": settings.tiktok_app_key,
        "app_secret": settings.tiktok_app_secret,
        "auth_code": auth_code,
        "grant_type": "authorized_code",
    }, timeout=30.0)
    return _unwrap(r)


def refresh_access_token(refresh_token: str) -> dict:
    import httpx

    r = httpx.get(f"{AUTH_BASE}/api/v2/token/refresh", params={
        "app_key": settings.tiktok_app_key,
        "app_secret": settings.tiktok_app_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout=30.0)
    return _unwrap(r)


def _unwrap(resp) -> dict:
    """TikTok wraps everything as {code, message, data}. code 0 == success."""
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") not in (0, "0", None):
        raise RuntimeError(f"TikTok API error {body.get('code')}: {body.get('message')}")
    return body.get("data") or {}


# ---- request signing (every data API call) ---------------------------------

def signed_params(path: str, params: dict, body: str = "") -> dict:
    """Return `params` plus app_key/timestamp/sign for a signed Open-API call.

    Recipe (TikTok Shop v2): sort the non-sign/non-token params, concat as
    `{key}{value}`, prepend the path, append the JSON body (if any), wrap with
    app_secret on both ends, then HMAC-SHA256 keyed by app_secret (hex)."""
    p = dict(params)
    p.setdefault("app_key", settings.tiktok_app_key)
    p.setdefault("timestamp", str(int(time.time())))

    parts = "".join(f"{k}{p[k]}" for k in sorted(p) if k not in ("sign", "access_token"))
    base = f"{settings.tiktok_app_secret}{path}{parts}{body}{settings.tiktok_app_secret}"
    p["sign"] = hmac.new(
        settings.tiktok_app_secret.encode(), base.encode(), hashlib.sha256
    ).hexdigest()
    return p


def get_authorized_shops(access_token: str) -> list[dict]:
    """Signed call to list the shops this token can act on (gives shop_cipher)."""
    import httpx

    params = signed_params(SHOPS_PATH, {})
    r = httpx.get(f"{OPEN_API_BASE}{SHOPS_PATH}", params=params,
                  headers={"x-tts-access-token": access_token,
                           "content-type": "application/json"}, timeout=30.0)
    return _unwrap(r).get("shops", []) or []


# ---- credential storage ----------------------------------------------------

def _expiry(value) -> datetime | None:
    """`*_expire_in` is either an absolute Unix timestamp or seconds-from-now —
    handle both defensively."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if v > 1_000_000_000:  # looks like an absolute Unix timestamp
        return datetime.utcfromtimestamp(v)
    return _utc_now_naive() + timedelta(seconds=v)


def store_credential(db: Session, token_data: dict, shop: dict | None) -> TikTokCredential:
    """Upsert the single TikTok credential row from a token payload (+ optional
    shop info from get_authorized_shops)."""
    cred = db.execute(select(TikTokCredential).order_by(TikTokCredential.id)).scalars().first()
    if cred is None:
        cred = TikTokCredential(access_token="", refresh_token="")
        db.add(cred)

    cred.access_token = token_data.get("access_token") or cred.access_token
    cred.refresh_token = token_data.get("refresh_token") or cred.refresh_token
    cred.access_expires_at = _expiry(token_data.get("access_token_expire_in")) or cred.access_expires_at
    cred.refresh_expires_at = _expiry(token_data.get("refresh_token_expire_in")) or cred.refresh_expires_at
    cred.seller_name = token_data.get("seller_name") or cred.seller_name
    cred.region = token_data.get("seller_base_region") or cred.region
    scopes = token_data.get("granted_scopes")
    if scopes:
        cred.granted_scopes = ",".join(scopes) if isinstance(scopes, list) else str(scopes)
    if shop:
        cred.shop_id = shop.get("id") or cred.shop_id
        cred.shop_cipher = shop.get("cipher") or cred.shop_cipher
        cred.shop_name = shop.get("name") or cred.shop_name
    return cred


def get_credential(db: Session) -> TikTokCredential | None:
    return db.execute(select(TikTokCredential).order_by(TikTokCredential.id)).scalars().first()


def ensure_fresh_token(db: Session, *, skew_minutes: int = 30) -> TikTokCredential | None:
    """Return the credential, refreshing the access token if it expires within
    `skew_minutes`. Callers use cred.access_token for data requests."""
    cred = get_credential(db)
    if cred is None:
        return None
    soon = _utc_now_naive() + timedelta(minutes=skew_minutes)
    if cred.access_expires_at is None or cred.access_expires_at <= soon:
        data = refresh_access_token(cred.refresh_token)
        store_credential(db, data, None)
        db.commit()
    return cred
