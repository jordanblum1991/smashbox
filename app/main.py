from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.auth import BasicAuthMiddleware, SessionAuthMiddleware, hash_password
from app.config import settings
from app.db import Base, SessionLocal, engine
from app.models import register_models  # noqa: F401  (side-effect: registers tables)
from app.models.user import User, UserRole
from app.routers import admin as admin_router
from app.routers import auth as auth_router
from app.routers import gmv_max_reimbursements as gmv_max_reimbursements_router
from app.routers import invoices as invoices_router
from app.routers import dashboard, exports, reports, uploads

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Smashbox", docs_url="/api/docs", redoc_url=None)

# Auto-create tables for v1. Switch to Alembic migrations before going to Postgres.
Base.metadata.create_all(bind=engine)


def _ensure_columns() -> None:
    """Minimal additive 'migrations' for SQLite — only used while we're still
    on Base.metadata.create_all. When a model gains a column, list it here so
    existing DBs pick it up on the next boot without dropping data."""
    from sqlalchemy import inspect, text
    # Phase 2: shop_id FK on every tenant-scoped table. Nullable so the
    # ALTER TABLE works without a default; the bootstrap migration backfills.
    shop_id_col = ("shop_id", "INTEGER REFERENCES shops(id)")
    needed = {
        "order_lines": [
            ("policy_violation_acknowledged", "BOOLEAN NOT NULL DEFAULT 0"),
            ("policy_violation_acknowledged_at", "DATETIME"),
        ],
        "orders": [
            shop_id_col,
            # TikTok-funded "Payment platform discount" — separate from
            # SKU Platform Discount. Subtracted in TikTok's GMV formula
            # under "Platform co-funding". Re-imports populate; existing
            # rows default to 0 until next CSV upload.
            ("payment_platform_discount", "NUMERIC(14,2) NOT NULL DEFAULT 0"),
        ],
        "settlements":          [shop_id_col],
        "adjustments":          [shop_id_col],
        "payouts":              [shop_id_col],
        "ad_spend":             [shop_id_col],
        "ad_credits": [
            shop_id_col,
            # applied_date is the new P&L windowing key. Backfilled to the 1st
            # of (year, month) for existing rows by _backfill_ad_credit_dates
            # immediately after this shim runs.
            ("applied_date", "DATE"),
        ],
        "samples":              [
            shop_id_col,
            ("shipping_cost", "NUMERIC(12,2)"),
            ("creator_id",    "INTEGER REFERENCES creators(id)"),
        ],
        "tiktok_daily_metrics": [shop_id_col],
        "import_batches":       [shop_id_col],
        "skus": [
            shop_id_col,
            # Procurement attributes (Phase A of demand planning).
            # Nullable; effective defaults applied at planner-compute time.
            ("lead_time_days", "INTEGER"),
            ("moq", "INTEGER"),
            ("case_pack", "INTEGER"),
            ("safety_stock_pct", "NUMERIC(5,2)"),
            ("is_reorderable", "BOOLEAN NOT NULL DEFAULT 1"),
            ("service_level", "NUMERIC(4,3)"),
        ],
        "bundles":              [shop_id_col],
        "users": [
            shop_id_col,
            ("is_super_admin", "BOOLEAN NOT NULL DEFAULT 0"),
        ],
    }
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, cols in needed.items():
            if not insp.has_table(table):
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols:
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


def _bootstrap_shop_and_backfill() -> None:
    """Phase 2 migration: ensure a 'smashbox' Shop row exists and backfill
    shop_id on every existing row of every tenant-scoped table. Idempotent —
    safe to run on every boot. Also promotes existing admins to super_admin
    so Phase 2c's cross-shop UI is reachable to whoever already had admin.

    Runs AFTER _ensure_columns (which has added the shop_id columns) and
    BEFORE _bootstrap_admin_user (so the bootstrapped admin gets a shop_id
    on creation rather than via this backfill)."""
    from sqlalchemy import text
    from app.models.shop import Shop
    from app.models.user import User, UserRole

    SHOP_SCOPED_TABLES = (
        "orders", "settlements", "adjustments", "payouts",
        "ad_spend", "ad_credits", "gmv_max_reimbursements",
        "samples", "tiktok_daily_metrics",
        "import_batches", "skus", "bundles",
    )

    with SessionLocal() as db:
        smashbox = db.query(Shop).filter_by(slug="smashbox").one_or_none()
        if smashbox is None:
            smashbox = Shop(
                slug="smashbox",
                name="Smashbox",
                timezone="America/Los_Angeles",
                is_active=True,
            )
            db.add(smashbox)
            db.commit()
            db.refresh(smashbox)

        # Backfill every shop-scoped table. We deliberately UPDATE only NULL
        # rows so future multi-shop data isn't overwritten when this re-runs.
        for table in SHOP_SCOPED_TABLES:
            db.execute(text(
                f"UPDATE {table} SET shop_id = :sid WHERE shop_id IS NULL"
            ), {"sid": smashbox.id})

        # Users: backfill shop_id and promote existing admins to super_admin.
        db.execute(text(
            "UPDATE users SET shop_id = :sid WHERE shop_id IS NULL"
        ), {"sid": smashbox.id})
        # SQLAlchemy's Enum column stores the enum NAME (e.g. "ADMIN"), not
        # the value ("admin") — use .name here, not .value.
        db.execute(text(
            "UPDATE users SET is_super_admin = 1 "
            "WHERE role = :admin AND is_super_admin = 0"
        ), {"admin": UserRole.ADMIN.name})

        db.commit()


def _backfill_ad_credit_dates() -> None:
    """One-time backfill: assign applied_date = date(year, month, 1) to every
    AdCredit row that doesn't have one yet. Conservative choice — places each
    legacy credit on the 1st of its month so monthly/YTD/yearly P&L totals are
    identical pre- and post-migration. Custom-range P&Ls will differ (intended,
    that's the whole point of moving to date-granularity).

    Idempotent: the WHERE clause guards against re-overwriting any date the
    user has since edited. Safe to run on every boot."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    if not insp.has_table("ad_credits"):
        return
    # `applied_date` may not exist on a very old DB where _ensure_columns
    # hasn't yet added it (shouldn't happen in practice since this runs after
    # the shim, but be defensive).
    cols = {c["name"] for c in insp.get_columns("ad_credits")}
    if "applied_date" not in cols:
        return
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE ad_credits "
            "SET applied_date = date(printf('%04d-%02d-01', year, month)) "
            "WHERE applied_date IS NULL"
        ))


_ensure_columns()
_backfill_ad_credit_dates()
_bootstrap_shop_and_backfill()


def _bootstrap_admin_user() -> None:
    """First-time setup. When no User rows exist and the operator has set
    INITIAL_ADMIN_EMAIL + INITIAL_ADMIN_PASSWORD, create that user as admin
    so the deployer can actually log in. After the first user exists, these
    env vars are ignored (idempotent on subsequent restarts).
    """
    if not (settings.initial_admin_email and settings.initial_admin_password):
        return
    from app.models.shop import Shop
    with SessionLocal() as db:
        if db.query(User).count() > 0:
            return
        # The smashbox shop is guaranteed to exist by _bootstrap_shop_and_backfill.
        smashbox = db.query(Shop).filter_by(slug="smashbox").one()
        admin = User(
            email=settings.initial_admin_email.lower().strip(),
            name=settings.initial_admin_name,
            password_hash=hash_password(settings.initial_admin_password),
            role=UserRole.ADMIN,
            is_super_admin=True,  # seed admin gets cross-shop access by default
            shop_id=smashbox.id,
            is_active=True,
        )
        db.add(admin)
        db.commit()


_bootstrap_admin_user()

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# Middleware order: outermost runs first. SessionMiddleware must be added
# BEFORE SessionAuthMiddleware so the latter has `request.session` available.
# Starlette inverts the registration order, so we add SessionAuthMiddleware
# first and SessionMiddleware second to get [Session → SessionAuth] inbound.
if settings.session_secret:
    app.add_middleware(SessionAuthMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        same_site="lax",
        https_only=True,
        max_age=14 * 24 * 3600,  # 14 days
    )
else:
    # No SESSION_SECRET set → fall back to the legacy Basic Auth gate (if
    # configured) or wide-open (local dev).
    app.add_middleware(BasicAuthMiddleware)


@app.middleware("http")
async def attach_data_health(request: Request, call_next):
    """Compute the Data Health counts once per request so nav.html can show
    a red-flag badge on the Data Health dropdown. Failures are swallowed —
    rendering the nav must never depend on the diagnostic queries succeeding."""
    request.state.data_health = {"unmapped": 0, "orphans": 0, "policy_violations": 0}
    if not request.url.path.startswith("/static"):
        try:
            from app.reports.policy_violations import count_policy_violations
            from app.reports.settlement_only_orders import count_settlement_only_orders
            from app.reports.unmapped_skus import count_unmapped_skus
            with SessionLocal() as db:
                request.state.data_health = {
                    "unmapped": count_unmapped_skus(db),
                    "orphans": count_settlement_only_orders(db),
                    "policy_violations": count_policy_violations(db),
                }
        except Exception:
            pass
    return await call_next(request)


app.include_router(auth_router.router)
app.include_router(admin_router.router)
app.include_router(gmv_max_reimbursements_router.router)
app.include_router(invoices_router.router)
app.include_router(dashboard.router)
app.include_router(uploads.router)
app.include_router(reports.router)
app.include_router(exports.router)
