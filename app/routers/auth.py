"""Login / logout routes for the Phase 1 per-user auth system.

GET  /login   — render the form (optional ?next= to bounce back after auth)
POST /login   — verify email + password, set session, update last_login_at
POST /logout  — clear the session and redirect to /login

These three paths are exempt from SessionAuthMiddleware, so the user can
actually reach them without being logged in.
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import verify_password
from app.db import get_db
from app.models.import_batch import _utc_now_naive
from app.models.user import User
from app.templating import templates

router = APIRouter(tags=["auth"])


@router.get("/login")
def login_page(request: Request, next: str = "/", error: str | None = None):
    """The form. Pre-fills the redirect target via ?next=… so users land
    back on the page that bounced them out (e.g. /reports/pnl)."""
    return templates.TemplateResponse(
        request, "login.html", {"next": next, "error": error}
    )


@router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(default="/"),
    db: Session = Depends(get_db),
):
    """Verify credentials, drop a session cookie, mark last_login_at.

    Failed lookups and failed password checks return the SAME generic error
    message so an attacker can't enumerate which emails are registered.
    """
    user = db.execute(
        select(User).where(User.email == email.lower().strip())
    ).scalar_one_or_none()

    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": next, "error": "Incorrect email or password."},
            status_code=401,
        )

    request.session["user_id"] = user.id
    user.last_login_at = _utc_now_naive()
    db.commit()

    # `next` could be tampered with; only allow same-site relative paths.
    safe_next = next if next.startswith("/") and not next.startswith("//") else "/"
    return RedirectResponse(url=safe_next, status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
