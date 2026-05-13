from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.db import Base, engine
from app.models import register_models  # noqa: F401  (side-effect: registers tables)
from app.routers import dashboard, exports, reports, uploads

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Smashbox", docs_url="/api/docs", redoc_url=None)

# Auto-create tables for v1. Switch to Alembic migrations before going to Postgres.
Base.metadata.create_all(bind=engine)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

app.include_router(dashboard.router)
app.include_router(uploads.router)
app.include_router(reports.router)
app.include_router(exports.router)
