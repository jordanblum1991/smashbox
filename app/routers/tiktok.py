"""TikTok Shop connection — authorize flow + status page.

  GET /admin/tiktok           status page (admin) + Connect button
  GET /auth/tiktok/authorize  redirect to TikTok's seller authorization
  GET /auth/tiktok/callback   exchange the returned code → store tokens

`/auth/tiktok/*` is exempt from SessionAuthMiddleware (TikTok calls the callback
without a session). Connecting is otherwise admin-gated via the status page.
"""
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.config import settings
from app.db import get_db
from app.services import tiktok_api
from app.templating import templates

router = APIRouter(tags=["tiktok"])

AUTHORIZE_URL = "https://services.tiktokshop.com/open/authorize"


@router.get("/admin/tiktok", dependencies=[Depends(require_admin)])
def tiktok_status(request: Request, db: Session = Depends(get_db),
                  error: str | None = None, notice: str | None = None):
    cred = tiktok_api.get_credential(db)
    return templates.TemplateResponse(
        request, "admin/tiktok.html",
        {"cred": cred, "configured": settings.tiktok_oauth_enabled,
         "error": error, "notice": notice},
    )


@router.get("/auth/tiktok/authorize")
def tiktok_authorize():
    if not settings.tiktok_oauth_enabled:
        return RedirectResponse("/admin/tiktok?error=TikTok+app+is+not+configured.", status_code=303)
    return RedirectResponse(
        f"{AUTHORIZE_URL}?{urlencode({'service_id': settings.tiktok_service_id})}",
        status_code=303,
    )


@router.get("/auth/tiktok/callback")
def tiktok_callback(code: str = "", db: Session = Depends(get_db)):
    if not settings.tiktok_oauth_enabled:
        return RedirectResponse("/admin/tiktok", status_code=303)
    if not code:
        return _back(error="No authorization code returned by TikTok.")
    try:
        token_data = tiktok_api.exchange_auth_code(code)
        # Discovering the shop_cipher is a signed call — best-effort so a token
        # capture still succeeds even if signing needs a tweak.
        shop = None
        try:
            shops = tiktok_api.get_authorized_shops(token_data.get("access_token", ""))
            shop = shops[0] if shops else None
        except Exception:  # noqa: BLE001
            pass
        tiktok_api.store_credential(db, token_data, shop)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        return _back(error=f"Token exchange failed: {exc}")
    return _back(notice="TikTok Shop connected.")


def _back(*, error: str | None = None, notice: str | None = None) -> RedirectResponse:
    params = {k: v for k, v in (("error", error), ("notice", notice)) if v}
    qs = f"?{urlencode(params)}" if params else ""
    return RedirectResponse(f"/admin/tiktok{qs}", status_code=303)
