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
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import hash_password, require_admin
from app.config import SERVICE_LEVEL_Z_TABLE
from app.db import get_db
from app.models.bundle import Bundle, BundleComponent
from app.models.sku import Sku
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


# ---------------------------------------------------------------------------
# Bundle catalog — admin-only list + add.
# Edit / delete are deferred per scope; XLSX upload path (Imports → Bundle
# Mapping) still writes to the same Bundle table.
# ---------------------------------------------------------------------------

@router.get("/bundles", dependencies=[Depends(require_admin)])
def bundles_page(
    request: Request,
    db: Session = Depends(get_db),
    error: str | None = None,
    notice: str | None = None,
):
    """List all bundles + render the add-bundle form. A-Z by name."""
    bundles = db.execute(
        select(Bundle).order_by(Bundle.name.asc())
    ).scalars().all()
    return templates.TemplateResponse(
        request,
        "admin/bundles.html",
        {"bundles": bundles, "error": error, "notice": notice},
    )


@router.post("/bundles", dependencies=[Depends(require_admin)])
def create_bundle(
    name: str = Form(...),
    variation: str | None = Form(default=None),
    tiktok_sku_id: str = Form(...),
    brand: str | None = Form(default=None),
    active_status: str = Form(default="Active"),
    msrp: str = Form(default=""),
    selling_price: str = Form(default=""),
    component_sku: list[str] = Form(default=[]),
    component_name: list[str] = Form(default=[]),
    component_qty: list[str] = Form(default=[]),
    component_msrp: list[str] = Form(default=[]),
    component_cogs: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    """Create a new bundle with N components.

    Free-text component SKUs match the importer's permissive behavior. Numeric
    fields REJECT invalid input — never silently coerced — because bundle COGS
    feeds P&L margins. Silent coercion to 0 would overstate margins on every
    order containing the bundle, invisibly.
    """
    name = name.strip()
    tiktok_sku_id = tiktok_sku_id.strip()
    brand = (brand or "").strip() or None
    variation = (variation or "").strip() or None
    if active_status not in ("Active", "Inactive"):
        active_status = "Active"

    if not name:
        return _bundles_back(error="Bundle name is required.")
    if not tiktok_sku_id:
        return _bundles_back(error="TikTok SKU ID is required.")

    existing = db.execute(
        select(Bundle).where(Bundle.tiktok_sku_id == tiktok_sku_id)
    ).scalar_one_or_none()
    if existing is not None:
        return _bundles_back(error=f"A bundle with TikTok SKU ID {tiktok_sku_id} already exists.")

    try:
        bundle_msrp = _money_strict(msrp, "Bundle MSRP")
        bundle_price = _money_strict(selling_price, "Bundle Selling Price")
    except ValueError as e:
        return _bundles_back(error=str(e))

    # Zip components. Rows with a blank SKU are skipped — supports the UI
    # adding extra empty rows that the user didn't fill in.
    n = max(len(component_sku), len(component_name), len(component_qty),
            len(component_msrp), len(component_cogs), 0)
    components: list[dict] = []
    for i in range(n):
        sku = (component_sku[i] if i < len(component_sku) else "").strip()
        if not sku:
            continue
        c_name = (component_name[i] if i < len(component_name) else "").strip() or None
        try:
            qty = _qty_strict(component_qty[i] if i < len(component_qty) else "", f"Component {i+1} quantity")
            c_msrp = _money_strict(component_msrp[i] if i < len(component_msrp) else "", f"Component {i+1} MSRP")
            c_cogs = _money_strict(component_cogs[i] if i < len(component_cogs) else "", f"Component {i+1} COGS")
        except ValueError as e:
            return _bundles_back(error=str(e))
        components.append({"sku": sku, "name": c_name, "qty": qty, "msrp": c_msrp, "cogs": c_cogs})

    if not components:
        return _bundles_back(error="At least one component is required (with a non-empty SKU).")

    # Synthesize bundle_sku from first component — matches importer behavior
    # so web-created and XLSX-imported bundles have the same shape.
    bundle_sku = components[0]["sku"] + "-BUNDLE"

    bundle = Bundle(
        tiktok_sku_id=tiktok_sku_id,
        bundle_sku=bundle_sku,
        name=name,
        variation=variation,
        brand=brand or "unknown",
        is_active=active_status,
        msrp=bundle_msrp,
        selling_price=bundle_price,
    )
    db.add(bundle)
    db.flush()
    for c in components:
        db.add(BundleComponent(
            bundle_id=bundle.id,
            component_sku=c["sku"],
            component_name=c["name"],
            quantity=c["qty"],
            msrp=c["msrp"],
            unit_cogs=c["cogs"],
        ))
    db.commit()
    return _bundles_back(notice=f"Added bundle: {name}")


def _bundles_back(*, error: str | None = None, notice: str | None = None) -> RedirectResponse:
    """303 back to /admin/bundles with error/notice flash. Uses urlencode so
    messages with quotes/colons/spaces survive cleanly."""
    qs: dict[str, str] = {}
    if error:
        qs["error"] = error
    if notice:
        qs["notice"] = notice
    suffix = ("?" + urlencode(qs)) if qs else ""
    return RedirectResponse(f"/admin/bundles{suffix}", status_code=303)


def _money_strict(s: str, label: str, *, min_value: Decimal | None = None) -> Decimal:
    """Parse money input. Blank → 0 (means 'not specified'). Non-numeric or
    below min_value → ValueError. Matches the ad-spend rejection rule: silent
    coercion to 0 would invisibly understate COGS/margins on every order
    using this bundle."""
    s = (s or "").strip()
    if not s:
        return Decimal("0")
    try:
        v = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"{label} must be a number (got '{s}').")
    if min_value is not None and v < min_value:
        raise ValueError(f"{label} must be at least {min_value} (got {v}).")
    return v


def _int_strict_optional(
    s: str,
    label: str,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int | None:
    """Parse optional integer input. Blank → None ('not specified'). Non-integer
    or out-of-range → ValueError. Used for procurement attrs where blank means
    'use the planner's global default' (NOT zero)."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        v = int(s)
    except ValueError:
        raise ValueError(f"{label} must be a whole number (got '{s}').")
    if min_value is not None and v < min_value:
        raise ValueError(f"{label} must be at least {min_value} (got {v}).")
    if max_value is not None and v > max_value:
        raise ValueError(f"{label} must be at most {max_value} (got {v}).")
    return v


def _decimal_strict_optional(
    s: str,
    label: str,
    *,
    min_value: Decimal | None = None,
    max_value: Decimal | None = None,
) -> Decimal | None:
    """Parse optional decimal input. Blank → None. Non-numeric or out-of-range
    → ValueError. Used for safety_stock_pct (range [0, 100]) and other
    nullable decimal fields where the absence of a value is meaningful."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        v = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"{label} must be a number (got '{s}').")
    if min_value is not None and v < min_value:
        raise ValueError(f"{label} must be at least {min_value} (got {v}).")
    if max_value is not None and v > max_value:
        raise ValueError(f"{label} must be at most {max_value} (got {v}).")
    return v


def _qty_strict(s: str, label: str) -> int:
    """Parse a quantity. Blank → 1 (matches importer default). Non-integer
    or < 1 → ValueError."""
    s = (s or "").strip()
    if not s:
        return 1
    try:
        v = int(s)
    except ValueError:
        raise ValueError(f"{label} must be a whole number (got '{s}').")
    if v < 1:
        raise ValueError(f"{label} must be at least 1.")
    return v


# ---------------------------------------------------------------------------
# SKU master — admin-only list + add.
# Edit / delete deferred. Existing in-place procurement editor at
# /reports/demand-planning/sku/{sku}/procurement continues to handle edits
# for already-created SKUs.
#
# Uniqueness rule (Option A, matches importer semantics):
#   - tiktok_sku_id, when provided, must be unique (DB-enforced).
#   - sku (SBX-form) may repeat across rows when each has a distinct
#     tiktok_sku_id — that's the TikTok-variation case.
#   - BUT: (sku, tiktok_sku_id IS NULL) must be unique — otherwise we'd
#     create two indistinguishable rows for the same product with no TikTok
#     ID. Mirrors the importer's fallback upsert key.
# ---------------------------------------------------------------------------

@router.get("/skus", dependencies=[Depends(require_admin)])
def skus_page(
    request: Request,
    db: Session = Depends(get_db),
    error: str | None = None,
    notice: str | None = None,
):
    """List all SKUs + render the add-SKU form. A-Z by name, then SBX code."""
    skus = db.execute(
        select(Sku).order_by(Sku.name.asc(), Sku.sku.asc())
    ).scalars().all()
    return templates.TemplateResponse(
        request,
        "admin/skus.html",
        {
            "skus": skus,
            "error": error,
            "notice": notice,
            "service_level_choices": sorted(SERVICE_LEVEL_Z_TABLE.keys()),
        },
    )


@router.post("/skus", dependencies=[Depends(require_admin)])
def create_sku(
    name: str = Form(...),
    sku: str = Form(...),
    tiktok_alt_sku: str | None = Form(default=None),
    tiktok_sku_id: str | None = Form(default=None),
    brand: str | None = Form(default=None),
    category: str | None = Form(default=None),
    item_type: str | None = Form(default=None),
    msrp: str = Form(default=""),
    unit_cogs: str = Form(default=""),
    active_status: str = Form(default="Active"),
    lead_time_days: str = Form(default=""),
    moq: str = Form(default=""),
    case_pack: str = Form(default=""),
    safety_stock_pct: str = Form(default=""),
    service_level: str = Form(default=""),
    is_reorderable: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    """Create a new Sku row. Numeric fields REJECT garbage; never silent-coerce.
    unit_cogs in particular feeds reorder math and P&L gross profit — coercion
    to 0 on a fat-fingered value would invisibly inflate margins."""
    name = name.strip()
    # Strip whitespace AND common paste artifacts (commas/semicolons/periods)
    # from identifier fields. A trailing comma from a copied CSV cell would
    # otherwise produce a row indistinguishable from a "clean" one but with
    # a different string value, defeating the uniqueness check downstream.
    _IDENT_TRIM = " \t\n\r,;."
    sku_code = sku.strip(_IDENT_TRIM)
    tiktok_alt_sku = (tiktok_alt_sku or "").strip(_IDENT_TRIM) or None
    tiktok_sku_id = (tiktok_sku_id or "").strip(_IDENT_TRIM) or None
    brand = (brand or "").strip() or "unknown"      # matches importer default
    category = (category or "").strip() or None
    item_type = (item_type or "").strip() or None
    if active_status not in ("Active", "Inactive"):
        active_status = "Active"
    is_active = (active_status == "Active")
    is_reorderable_bool = is_reorderable is not None

    if not name:
        return _skus_back(error="Name is required.")
    if not sku_code:
        return _skus_back(error="SKU code is required.")

    # TikTok SKU IDs are numeric. Reject any non-digit chars that survived
    # the strip — catches embedded commas, letters, typos pasted from CSVs.
    if tiktok_sku_id is not None and not tiktok_sku_id.isdigit():
        return _skus_back(
            error=f"TikTok SKU ID must contain only digits (got '{tiktok_sku_id}')."
        )

    # Uniqueness — two-tier per Option A.
    if tiktok_sku_id:
        clash = db.execute(
            select(Sku).where(Sku.tiktok_sku_id == tiktok_sku_id)
        ).scalar_one_or_none()
        if clash is not None:
            return _skus_back(
                error=f"A SKU with TikTok SKU ID {tiktok_sku_id} already exists "
                      f"(SBX code: {clash.sku})."
            )
    else:
        clash = db.execute(
            select(Sku).where(Sku.sku == sku_code).where(Sku.tiktok_sku_id.is_(None))
        ).scalar_one_or_none()
        if clash is not None:
            return _skus_back(
                error=f"A SKU with code {sku_code} (and no TikTok SKU ID) already "
                      f"exists. To add a new TikTok variation, provide a TikTok SKU ID."
            )

    try:
        msrp_dec = _money_strict(msrp, "MSRP", min_value=Decimal("0"))
        cogs_dec = _money_strict(unit_cogs, "Unit COGS", min_value=Decimal("0"))
        lead_time = _int_strict_optional(lead_time_days, "Lead time (days)", min_value=0)
        moq_int = _int_strict_optional(moq, "MOQ", min_value=0)
        case_pack_int = _int_strict_optional(case_pack, "Case pack", min_value=0)
        safety_pct = _decimal_strict_optional(
            safety_stock_pct, "Safety stock %",
            min_value=Decimal("0"), max_value=Decimal("100"),
        )
    except ValueError as e:
        return _skus_back(error=str(e))

    # Service level — blank OR one of the table-allowed values. The dropdown
    # only offers valid choices; this catches browser/curl bypass.
    service_level_dec: Decimal | None = None
    if service_level.strip():
        try:
            cand = Decimal(service_level.strip())
        except InvalidOperation:
            return _skus_back(error=f"Service level must be a decimal (got '{service_level}').")
        if cand not in SERVICE_LEVEL_Z_TABLE:
            allowed = ", ".join(str(k) for k in sorted(SERVICE_LEVEL_Z_TABLE.keys()))
            return _skus_back(error=f"Service level must be one of: {allowed} (or blank).")
        service_level_dec = cand

    db.add(Sku(
        sku=sku_code,
        tiktok_alt_sku=tiktok_alt_sku,
        tiktok_sku_id=tiktok_sku_id,
        name=name,
        brand=brand,
        category=category,
        item_type=item_type,
        msrp=msrp_dec,
        unit_cogs=cogs_dec,
        is_active=is_active,
        lead_time_days=lead_time,
        moq=moq_int,
        case_pack=case_pack_int,
        safety_stock_pct=safety_pct,
        is_reorderable=is_reorderable_bool,
        service_level=service_level_dec,
    ))
    db.commit()
    return _skus_back(notice=f"Added SKU: {name}")


def _skus_back(*, error: str | None = None, notice: str | None = None) -> RedirectResponse:
    """303 back to /admin/skus with error/notice flash. urlencode'd."""
    qs: dict[str, str] = {}
    if error:
        qs["error"] = error
    if notice:
        qs["notice"] = notice
    suffix = ("?" + urlencode(qs)) if qs else ""
    return RedirectResponse(f"/admin/skus{suffix}", status_code=303)
