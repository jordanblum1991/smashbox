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

from app.auth import hash_password, verify_password
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


# ---- Self-service password change ----------------------------------------

@router.get("/account/password")
def password_page(request: Request, error: str | None = None, notice: str | None = None):
    """Render the change-password form. Auth middleware ensures the user is
    signed in before reaching this route."""
    return templates.TemplateResponse(
        request, "account/password.html", {"error": error, "notice": notice},
    )


@router.post("/account/password")
def password_submit(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    """Verify the user's current password, then rotate to the new one.

    Re-fetches the User from the DB rather than trusting request.state.user
    because we need a session-bound object to mutate. The session cookie
    stays valid post-change — no forced re-login.
    """
    user_id = request.session.get("user_id")
    if user_id is None:
        # Auth middleware should've caught this, but belt + suspenders
        return RedirectResponse(url="/login", status_code=303)
    user = db.get(User, user_id)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)

    if not verify_password(current_password, user.password_hash):
        return templates.TemplateResponse(
            request, "account/password.html",
            {"error": "Current password is incorrect."},
            status_code=400,
        )
    if len(new_password) < 8:
        return templates.TemplateResponse(
            request, "account/password.html",
            {"error": "New password must be at least 8 characters."},
            status_code=400,
        )
    if new_password != confirm_password:
        return templates.TemplateResponse(
            request, "account/password.html",
            {"error": "New password and confirmation do not match."},
            status_code=400,
        )
    if new_password == current_password:
        return templates.TemplateResponse(
            request, "account/password.html",
            {"error": "New password must be different from the current one."},
            status_code=400,
        )

    user.password_hash = hash_password(new_password)
    db.commit()

    return templates.TemplateResponse(
        request, "account/password.html",
        {"notice": "Password changed. Use it the next time you sign in."},
    )
