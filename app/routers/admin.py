"""Admin-only user management (Phase 1b).

Routes gated by `require_admin`. UI lives at /admin/users with an inline
add-user form, per-row edit/reset-password forms, and an activate/deactivate
toggle.

Safety guards:
- A user cannot deactivate themselves (lockout risk).
- A user cannot demote themselves from admin to member.
- The last active admin cannot be deactivated or demoted (system-wide
  lockout). Enforced at the route level.

We intentionally do NOT support hard delete — users are deactivated. This
preserves audit trails (e.g. `policy_violation_acknowledged_at` rows that
point at the user implicitly via the batch.import_batch_id chain) and avoids
FK churn.
"""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import hash_password, require_admin
from app.db import get_db
from app.models.user import User, UserRole
from app.templating import templates

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/users", dependencies=[Depends(require_admin)])
def users_page(
    request: Request,
    db: Session = Depends(get_db),
    error: str | None = None,
    notice: str | None = None,
):
    users = db.execute(
        select(User).order_by(User.is_active.desc(), User.created_at.desc())
    ).scalars().all()
    return templates.TemplateResponse(
        request,
        "admin/users.html",
        {"users": users, "error": error, "notice": notice},
    )


@router.post("/users", dependencies=[Depends(require_admin)])
def create_user(
    request: Request,
    email: str = Form(...),
    name: str = Form(...),
    password: str = Form(...),
    role: str = Form(default="member"),
    db: Session = Depends(get_db),
):
    email = email.lower().strip()
    name = name.strip()

    if not email or not name or not password:
        return _back(request, error="Email, name, and password are required.")
    if len(password) < 8:
        return _back(request, error="Password must be at least 8 characters.")
    if role not in {r.value for r in UserRole}:
        return _back(request, error="Invalid role.")

    existing = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if existing is not None:
        return _back(request, error=f"A user with email {email} already exists.")

    db.add(User(
        email=email,
        name=name,
        password_hash=hash_password(password),
        role=UserRole(role),
        is_active=True,
    ))
    db.commit()
    return _back(request, notice=f"Created user {email}.")


@router.post("/users/{user_id}/edit", dependencies=[Depends(require_admin)])
def edit_user(
    user_id: int,
    request: Request,
    name: str = Form(...),
    role: str = Form(...),
    is_active: str | None = Form(default=None),  # checkbox: "on" or absent
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")

    current = request.state.user
    new_role = UserRole(role) if role in {r.value for r in UserRole} else target.role
    new_active = is_active is not None

    # Self-modification guards
    if current and current.id == target.id:
        if not new_active:
            return _back(request, error="You cannot deactivate your own account.")
        if new_role != UserRole.ADMIN:
            return _back(request, error="You cannot remove your own admin role.")

    # Last-admin guard — applies even when an admin edits a DIFFERENT admin
    if target.role == UserRole.ADMIN and (
        new_role != UserRole.ADMIN or not new_active
    ):
        active_admins = db.execute(
            select(func.count(User.id))
            .where(User.role == UserRole.ADMIN)
            .where(User.is_active.is_(True))
        ).scalar() or 0
        if active_admins <= 1:
            return _back(request, error=(
                "Cannot demote or deactivate the last active admin. "
                "Promote another user to admin first."
            ))

    target.name = name.strip() or target.name
    target.role = new_role
    target.is_active = new_active
    db.commit()
    return _back(request, notice=f"Updated {target.email}.")


@router.post("/users/{user_id}/reset-password", dependencies=[Depends(require_admin)])
def reset_password(
    user_id: int,
    request: Request,
    new_password: str = Form(...),
    db: Session = Depends(get_db),
):
    if len(new_password) < 8:
        return _back(request, error="New password must be at least 8 characters.")

    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")

    target.password_hash = hash_password(new_password)
    db.commit()
    return _back(request, notice=f"Reset password for {target.email}.")


def _back(request: Request, *, error: str | None = None, notice: str | None = None) -> RedirectResponse:
    """Redirect back to /admin/users with a flash-message query string. We use
    query params (not flask-style flash) so the message is bookmark-able and
    survives the 303 cleanly without needing extra session storage."""
    qs = []
    if error:
        qs.append(f"error={error}")
    if notice:
        qs.append(f"notice={notice}")
    return RedirectResponse(
        url=f"/admin/users{('?' + '&'.join(qs)) if qs else ''}",
        status_code=303,
    )
