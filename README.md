# Smashbox

Internal full-stack web app for managing the Smashbox TikTok Shop P&L and operating reports.

Upload TikTok Shop export files, get accurate monthly P&L, SKU-level profitability, sample tracking, and reconciliation summaries.

## Quick start

```bash
py -m venv .venv
.\.venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000.

## Run tests

```bash
pytest
pytest tests/test_seller_funded_split.py -v   # one file
pytest -k split                                # by keyword
```

## What lives where

See `CLAUDE.md` for the architecture map.

## Stack

FastAPI · Jinja2 + HTMX · Tailwind (CDN) · SQLAlchemy + Alembic · SQLite (Postgres-ready) · pandas · openpyxl / xlsxwriter
