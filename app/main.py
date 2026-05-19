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
    needed = {
        "order_lines": [
            ("policy_violation_acknowledged", "BOOLEAN NOT NULL DEFAULT 0"),
            ("policy_violation_acknowledged_at", "DATETIME"),
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


_ensure_columns()


def _bootstrap_admin_user() -> None:
    """First-time setup. When no User rows exist and the operator has set
    INITIAL_ADMIN_EMAIL + INITIAL_ADMIN_PASSWORD, create that user as admin
    so the deployer can actually log in. After the first user exists, these
    env vars are ignored (idempotent on subsequent restarts).
    """
    if not (settings.initial_admin_email and settings.initial_admin_password):
        return
    with SessionLocal() as db:
        if db.query(User).count() > 0:
            return
        admin = User(
            email=settings.initial_admin_email.lower().strip(),
            name=settings.initial_admin_name,
            password_hash=hash_password(settings.initial_admin_password),
            role=UserRole.ADMIN,
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
app.include_router(dashboard.router)
app.include_router(uploads.router)
app.include_router(reports.router)
app.include_router(exports.router)
