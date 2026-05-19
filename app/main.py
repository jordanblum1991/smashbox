from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from app.auth import BasicAuthMiddleware
from app.db import Base, SessionLocal, engine
from app.models import register_models  # noqa: F401  (side-effect: registers tables)
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

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# Auth must be registered BEFORE the data-health middleware so unauth requests
# short-circuit without doing DB work.
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


app.include_router(dashboard.router)
app.include_router(uploads.router)
app.include_router(reports.router)
app.include_router(exports.router)
