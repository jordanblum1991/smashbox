from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from app.db import Base, SessionLocal, engine
from app.models import register_models  # noqa: F401  (side-effect: registers tables)
from app.routers import dashboard, exports, reports, uploads

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Smashbox", docs_url="/api/docs", redoc_url=None)

# Auto-create tables for v1. Switch to Alembic migrations before going to Postgres.
Base.metadata.create_all(bind=engine)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.middleware("http")
async def attach_data_health(request: Request, call_next):
    """Compute the two Data Health counts once per request so nav.html can show
    a red-flag badge on the Data Health dropdown. Failures are swallowed —
    rendering the nav must never depend on the diagnostic queries succeeding."""
    request.state.data_health = {"unmapped": 0, "orphans": 0}
    if not request.url.path.startswith("/static"):
        try:
            from app.reports.settlement_only_orders import count_settlement_only_orders
            from app.reports.unmapped_skus import count_unmapped_skus
            with SessionLocal() as db:
                request.state.data_health = {
                    "unmapped": count_unmapped_skus(db),
                    "orphans": count_settlement_only_orders(db),
                }
        except Exception:
            pass
    return await call_next(request)


app.include_router(dashboard.router)
app.include_router(uploads.router)
app.include_router(reports.router)
app.include_router(exports.router)
