"""TikTok Marketing API client (business-api.tiktok.com) — advertiser ad spend.

SEPARATE from app/services/tiktok_api.py (the Shop API). The Marketing API:
  - authorizes advertisers via the Business Center portal,
  - issues ONE long-lived access token (no refresh, no expiry to track),
  - authenticates data calls with an `Access-Token` HEADER (no HMAC signing).

Flow: authorize_url() -> advertiser consents -> /auth/tiktok-ads/callback gets
`auth_code` -> exchange_auth_code() -> store_credential(). Then get_ad_spend()
pulls the integrated report (campaign spend by day) for the fetcher.

Tokens are secrets — never logged or serialized into a response.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.tiktok_marketing_credential import TikTokMarketingCredential

BASE = "https://business-api.tiktok.com/open_api/v1.3"
AUTH_PORTAL = "https://business-api.tiktok.com/portal/auth"
_TIMEOUT = 40.0


def authorize_url(state: str) -> str:
    """Business Center advertiser-authorization URL. After consent TikTok
    redirects to the registered redirect_uri with ?auth_code=…&state=…."""
    return f"{AUTH_PORTAL}?" + urlencode({
        "app_id": settings.tiktok_marketing_app_id,
        "state": state,
        "redirect_uri": settings.tiktok_ads_redirect_uri,
    })


def _unwrap(resp) -> dict:
    """Unwrap TikTok's {code, message, data} envelope. code 0 == success."""
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") not in (0, "0", None):
        raise RuntimeError(f"TikTok Marketing API error {body.get('code')}: {body.get('message')}")
    return body.get("data") or {}


def exchange_auth_code(auth_code: str) -> dict:
    """Trade the advertiser auth_code for a long-lived access token.
    Returns data: {access_token, scope: [...], advertiser_ids: [...]}."""
    import httpx

    r = httpx.post(
        f"{BASE}/oauth2/access_token/",
        json={
            "app_id": settings.tiktok_marketing_app_id,
            "secret": settings.tiktok_marketing_secret,
            "auth_code": auth_code,
            "grant_type": "auth_code",
        },
        timeout=_TIMEOUT,
    )
    return _unwrap(r)


def get_advertisers(access_token: str) -> list[dict]:
    """List advertiser accounts this token can access: [{advertiser_id,
    advertiser_name}]. Best-effort — used only to label the connection."""
    import httpx

    r = httpx.get(
        f"{BASE}/oauth2/advertiser/get/",
        params={
            "app_id": settings.tiktok_marketing_app_id,
            "secret": settings.tiktok_marketing_secret,
        },
        headers={"Access-Token": access_token},
        timeout=_TIMEOUT,
    )
    return _unwrap(r).get("list", []) or []


def _to_decimal(v) -> Decimal:
    try:
        return abs(Decimal(str(v)))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def get_ad_spend(access_token: str, advertiser_id: str,
                 start_date: str, end_date: str) -> list[dict]:
    """Integrated BASIC report: campaign spend per day for [start_date, end_date]
    (inclusive, YYYY-MM-DD). Returns
    [{campaign_id, campaign_name, stat_day(str YYYY-MM-DD), spend(Decimal)}].
    Pages through the full result set."""
    import httpx

    from app.services.http_retry import send_with_retry

    out: list[dict] = []
    page = 1
    while True:
        r = send_with_retry(lambda: httpx.get(
            f"{BASE}/report/integrated/get/",
            params={
                "advertiser_id": advertiser_id,
                "report_type": "BASIC",
                "data_level": "AUCTION_CAMPAIGN",
                "dimensions": json.dumps(["campaign_id", "stat_time_day"]),
                "metrics": json.dumps(["spend", "campaign_name"]),
                "start_date": start_date,
                "end_date": end_date,
                "page": page,
                "page_size": 1000,
            },
            headers={"Access-Token": access_token},
            timeout=_TIMEOUT,
        ), label="marketing report")
        data = _unwrap(r)
        for item in data.get("list", []) or []:
            dims = item.get("dimensions") or {}
            mets = item.get("metrics") or {}
            stat = (dims.get("stat_time_day") or "")[:10]  # "YYYY-MM-DD 00:00:00" -> date
            cid = str(dims.get("campaign_id") or "").strip()
            if not stat or not cid:
                continue
            out.append({
                "campaign_id": cid,
                "campaign_name": mets.get("campaign_name"),
                "stat_day": stat,
                "spend": _to_decimal(mets.get("spend")),
            })
        page_info = data.get("page_info") or {}
        total_pages = int(page_info.get("total_page") or 1)
        if page >= total_pages:
            break
        page += 1
    return out


def store_credential(db: Session, token_data: dict,
                     advertisers: list[dict] | None) -> TikTokMarketingCredential:
    """Upsert the single Marketing API credential row from a token payload."""
    cred = db.execute(
        select(TikTokMarketingCredential).order_by(TikTokMarketingCredential.id)
    ).scalars().first()
    if cred is None:
        cred = TikTokMarketingCredential(access_token="")
        db.add(cred)

    cred.access_token = token_data.get("access_token") or cred.access_token
    ids = token_data.get("advertiser_ids") or []
    if ids:
        cred.advertiser_ids = ",".join(str(i) for i in ids)
        cred.advertiser_id = str(ids[0])
    scopes = token_data.get("scope")
    if scopes:
        cred.granted_scopes = ",".join(str(s) for s in scopes) if isinstance(scopes, list) else str(scopes)
    if advertisers:
        # Name the primary advertiser (or first available).
        primary = next((a for a in advertisers if str(a.get("advertiser_id")) == cred.advertiser_id), advertisers[0])
        cred.advertiser_name = primary.get("advertiser_name") or cred.advertiser_name
        if not cred.advertiser_id:
            cred.advertiser_id = str(primary.get("advertiser_id") or "") or None
    return cred


def get_credential(db: Session) -> TikTokMarketingCredential | None:
    return db.execute(
        select(TikTokMarketingCredential).order_by(TikTokMarketingCredential.id)
    ).scalars().first()


def advertiser_id_list(cred: TikTokMarketingCredential) -> list[str]:
    """All advertiser ids the credential grants (the fetcher pulls each)."""
    if cred.advertiser_ids:
        return [s for s in (x.strip() for x in cred.advertiser_ids.split(",")) if s]
    return [cred.advertiser_id] if cred.advertiser_id else []


def _api_get(path: str, params: dict, access_token: str) -> dict:
    """Single GET seam for Marketing-API reads — returns the unwrapped `data`
    dict. Isolated so tests stub it instead of hitting the network."""
    import httpx

    from app.services.http_retry import send_with_retry

    r = send_with_retry(lambda: httpx.get(
        f"{BASE}{path}", params=params,
        headers={"Access-Token": access_token}, timeout=_TIMEOUT,
    ), label=f"marketing {path}")
    return _unwrap(r)


def list_gmv_max_campaigns(access_token: str, advertiser_id: str) -> list[dict]:
    """All GMV-Max campaigns for the advertiser (paginated). Each row carries
    campaign_id / campaign_name / status — NOT a store id (see gmv_max_store_ids)."""
    out: list[dict] = []
    page = 1
    while True:
        data = _api_get("/gmv_max/campaign/get/", {
            "advertiser_id": advertiser_id,
            "filtering": json.dumps(
                {"gmv_max_promotion_types": ["PRODUCT_GMV_MAX", "LIVE_GMV_MAX"]}),
            "page": page, "page_size": 100,
        }, access_token)
        out.extend(data.get("list") or [])
        total = _int_metric((data.get("page_info") or {}).get("total_page")) or 1
        if page >= total:
            break
        page += 1
    return out


def gmv_max_store_ids(access_token: str, advertiser_id: str,
                      campaigns: list[dict]) -> list[str]:
    """Distinct TikTok-Shop store ids backing the GMV-Max campaigns. The report
    endpoint is store-keyed (returns all campaigns for a store), so we only need
    the unique set — one store today. Reads each campaign's `/campaign/gmv_max/
    info/` top-level `store_id`."""
    stores: list[str] = []
    for c in campaigns:
        cid = str(c.get("campaign_id") or "").strip()
        if not cid:
            continue
        data = _api_get("/campaign/gmv_max/info/",
                        {"advertiser_id": advertiser_id, "campaign_id": cid}, access_token)
        sid = str(data.get("store_id") or "").strip()
        if sid and sid not in stores:
            stores.append(sid)
    return stores


def get_gmv_max_report(access_token: str, advertiser_id: str, store_ids: list[str],
                       start_date: str, end_date: str) -> list[dict]:
    """Daily GMV-Max metrics per campaign for [start_date, end_date] (<=30 days,
    inclusive, YYYY-MM-DD). Returns
    [{stat_day(str), cost(Decimal), orders(int), gross_revenue(Decimal)}],
    paginated. Caller sums across campaigns/days."""
    out: list[dict] = []
    page = 1
    while True:
        data = _api_get("/gmv_max/report/get/", {
            "advertiser_id": advertiser_id,
            "store_ids": json.dumps(list(store_ids)),
            "dimensions": json.dumps(["campaign_id", "stat_time_day"]),
            "metrics": json.dumps(["cost", "orders", "gross_revenue"]),
            "start_date": start_date, "end_date": end_date,
            "page": page, "page_size": 1000,
        }, access_token)
        for item in data.get("list") or []:
            dims = item.get("dimensions") or {}
            mets = item.get("metrics") or {}
            stat = (dims.get("stat_time_day") or "")[:10]
            if not stat:
                continue
            out.append({
                "stat_day": stat,
                "cost": _to_decimal(mets.get("cost")),
                "orders": _int_metric(mets.get("orders")),
                "gross_revenue": _to_decimal(mets.get("gross_revenue")),
            })
        total = _int_metric((data.get("page_info") or {}).get("total_page")) or 1
        if page >= total:
            break
        page += 1
    return out


def _int_metric(v) -> int:
    try:
        return int(Decimal(str(v)))
    except (InvalidOperation, TypeError, ValueError):
        return 0


# Reuses TikTokSyncState under its own stream key, separate from the Shop
# streams (orders/settlements/payouts/analytics) since it's a different auth.
ADS_STREAM = "ads"


def run_ads_sync(db: Session, *, source: str = "manual") -> str:
    """Pull ad spend via the Marketing API into AdSpend. Never raises — records
    the outcome on TikTokSyncState(stream='ads'). Returns the status string
    ('ok' | 'empty' | 'pending' | 'error')."""
    from app.models.import_batch import _utc_now_naive
    from app.services.tiktok_fetchers import fetch_ad_spend
    from app.services.tiktok_sync import get_state

    state = get_state(db, ADS_STREAM)
    now = _utc_now_naive()
    state.last_run_at = now

    cred = get_credential(db)
    if cred is None or not cred.access_token:
        state.last_status = "pending"
        state.last_message = "TikTok Marketing API not connected — connect on this page first."
        db.commit()
        return "pending"
    try:
        # None on the first run → the fetcher backfills its 30-day window;
        # afterwards the watermark drives a trailing re-pull.
        n = fetch_ad_spend(db, cred, state.synced_through)
        state.synced_through = now
        state.rows_last_run = n
        state.last_status = "ok" if n else "empty"
        state.last_message = None
        result = state.last_status
    except Exception as exc:  # noqa: BLE001
        state.last_status = "error"
        state.last_message = str(exc)
        result = "error"
    db.commit()
    return result
