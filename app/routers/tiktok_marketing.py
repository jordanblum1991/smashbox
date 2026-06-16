"""TikTok Marketing API connection — advertiser ad-spend auth + status page.

  GET  /admin/tiktok-ads          status page (admin) + Connect button
  POST /admin/tiktok-ads/sync     manual "Sync ad spend now"
  GET  /auth/tiktok-ads/authorize redirect to the Business Center advertiser auth
  GET  /auth/tiktok-ads/callback  exchange the returned auth_code → store token

Separate from the Shop API connection (/admin/tiktok). `/auth/tiktok-ads/*` is
exempt from SessionAuthMiddleware via the existing "/auth/tiktok" prefix (TikTok
calls the callback without a session).
"""
import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.auth import require_admin
from app.config import settings
from app.db import get_db
from app.models.tiktok_sync_state import TikTokSyncState
from app.services import tiktok_marketing_api as mkt
from app.templating import templates

router = APIRouter(tags=["tiktok-ads"])


@router.get("/admin/tiktok-ads", dependencies=[Depends(require_admin)])
def tiktok_ads_status(request: Request, db: Session = Depends(get_db),
                      error: str | None = None, notice: str | None = None):
    ads_state = db.query(TikTokSyncState).filter_by(stream=mkt.ADS_STREAM).first()
    return templates.TemplateResponse(
        request, "admin/tiktok_marketing.html",
        {"cred": mkt.get_credential(db),
         "configured": settings.tiktok_marketing_oauth_enabled,
         "redirect_uri": settings.tiktok_ads_redirect_uri,
         "ads_state": ads_state,
         "error": error, "notice": notice},
    )


@router.post("/admin/tiktok-ads/sync", dependencies=[Depends(require_admin)])
async def tiktok_ads_sync_now(db: Session = Depends(get_db)):
    """Manual 'Sync ad spend now'. Runs off the event loop."""
    status = await run_in_threadpool(mkt.run_ads_sync, db, source="manual")
    if status == "pending":
        return _back(notice="Sync ran — pending until the Marketing API is connected.")
    if status == "error":
        return _back(error="Ad-spend sync failed — see the status panel.")
    return _back(notice=f"Ad-spend sync complete ({status}).")


@router.get("/auth/tiktok-ads/authorize")
def tiktok_ads_authorize():
    if not settings.tiktok_marketing_oauth_enabled:
        return _back(error="TikTok Marketing app is not configured.")
    if not settings.tiktok_ads_redirect_uri:
        return _back(error="Set PUBLIC_BASE_URL so the advertiser redirect URL can be built.")
    state = secrets.token_urlsafe(16)
    return RedirectResponse(mkt.authorize_url(state), status_code=303)


@router.get("/auth/tiktok-ads/callback")
def tiktok_ads_callback(auth_code: str = "", code: str = "",
                        db: Session = Depends(get_db)):
    if not settings.tiktok_marketing_oauth_enabled:
        return RedirectResponse("/admin/tiktok-ads", status_code=303)
    received = auth_code or code  # TikTok sends both `auth_code` and `code`
    if not received:
        return _back(error="No authorization code returned by TikTok.")
    try:
        token_data = mkt.exchange_auth_code(received)
        # Advertiser names are a labeling nicety — best-effort.
        advertisers = None
        try:
            advertisers = mkt.get_advertisers(token_data.get("access_token", ""))
        except Exception:  # noqa: BLE001
            pass
        mkt.store_credential(db, token_data, advertisers)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        return _back(error=f"Token exchange failed: {exc}")
    return _back(notice="TikTok Marketing API connected.")


def _back(*, error: str | None = None, notice: str | None = None) -> RedirectResponse:
    params = {k: v for k, v in (("error", error), ("notice", notice)) if v}
    qs = f"?{urlencode(params)}" if params else ""
    return RedirectResponse(f"/admin/tiktok-ads{qs}", status_code=303)
