# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Internal full-stack web app for managing the **Smashbox TikTok Shop P&L and operating reports**. Users (me and a small finance/ops team) upload TikTok Shop exports (orders, settlements, payouts) plus reference files (SKU master, bundle mapping, samples) and the app produces monthly P&L, YTD P&L, SKU-level profitability, sample tracking, and reconciliation against TikTok's raw numbers.

Private/internal — no public-facing surface, no auth in v1.

## Commands

```bash
py -m venv .venv && .\.venv\Scripts\activate   # Windows
pip install -r requirements.txt
copy .env.example .env

uvicorn app.main:app --reload                  # dev server at http://127.0.0.1:8000
pytest                                         # full test suite
pytest tests/test_seller_funded_split.py -v    # one file
pytest -k split                                # by keyword
```

Tables auto-create on first boot via `Base.metadata.create_all`. Switch to Alembic migrations (`alembic upgrade head`) before moving to Postgres — `alembic/` is reserved but not initialized yet.

## Python 3.14 quirk

Many libs only ship 3.14 wheels in their latest minor versions, so `requirements.txt` uses lower-bound (`>=`) pins, not equality. `pydantic-core` will try to build from Rust source if you pin an older version — don't.

## Architecture

Layered, all under `app/`:

```
routers/       FastAPI route handlers — HTTP in, template/file out
templates/     Jinja2 (HTMX-ready) — base.html + partials/nav.html + per-page files
static/        Tailwind via CDN; static/css/app.css holds only print rules
models/        SQLAlchemy 2.x ORM — one file per table
importers/     File parsers: BaseImporter + one subclass per ImportFileKind
reports/       Pure computation: takes a Session, returns dataclasses
rules/         Cross-cutting business rules (currently: seller_funded_split)
schemas/       (empty) Pydantic schemas — add when APIs stabilize
services/      (empty) Cross-cutting orchestration jobs
```

**Data flow:** upload → `routers/uploads.py` saves the file to `uploads/`, creates an `ImportBatch`, dispatches to the matching `BaseImporter` subclass. Every imported row carries `import_batch_id` so a bad import can be rolled back by deleting one batch.

**Reports never write.** They read from the ORM and return dataclasses; routers render them. Pure functions, easy to unit-test.

## Load-bearing invariant: seller-funded discount split

`app/rules/seller_funded_split.py` splits TikTok's seller-funded discount between Outlandish and Smashbox. **The two parts MUST sum to the original total — exactly, no rounding drift.** Reconciliation depends on this; `test_seller_funded_split.py` enforces it across odd-cent edge cases.

Implementation: compute the Outlandish share with banker's rounding to cents; assign the residual to Smashbox so the sum is exact by construction. Don't "fix" a failing test by relaxing the invariant — fix the split function.

Default split ratio is `SELLER_FUNDED_OUTLANDISH_SHARE` from `.env` (currently 0.5). Per-order or per-SKU overrides can be passed to `split_seller_funded_discount(total, outlandish_share=...)`.

## Order taxonomy

`Order.order_type` (`OrderType` enum): `PAID` / `SAMPLE` / `PAID_SAMPLE`. The P&L includes `PAID` only. `SAMPLE` and `PAID_SAMPLE` feed the sample-tracking report. Free samples count against `FREE_SAMPLE_MONTHLY_ALLOWANCE`; anything over becomes paid oversampling.

`Order.unit_cogs_snapshot` is captured at import time so historical reports don't shift when the SKU master is edited later. If the snapshot is zero (legacy row) the report falls back to current `Sku.unit_cogs`.

## Money

Always `Decimal`, never `float`. `Numeric(14, 2)` in the ORM; quantize to `0.01` when persisting. The `money` Jinja filter (`app/templating.py`) formats Decimals for display.

## Stack pointers

- FastAPI + Starlette 1.0 — **`TemplateResponse(request, "name.html", {...})`** — `request` is the first positional arg, NOT in the context dict. The old form silently turns the context into a Jinja cache key and crashes with "unhashable type: 'dict'".
- Jinja2 + HTMX (CDN). Tailwind via CDN — no build step.
- SQLAlchemy 2.x with `Mapped[...]` annotations. `app/db.py` exports `engine`, `SessionLocal`, `Base`, `get_db`.
- pandas for parsing imports; xlsxwriter for Excel export.

## Importers

`app/importers/__init__.py` has the `IMPORTERS: dict[ImportFileKind, type[BaseImporter]]` registry. Only `TIKTOK_ORDERS` is wired up; the rest are TODO. Each importer:

- Subclasses `BaseImporter`, implements `run(path, db, batch) -> ImportResult`.
- **Does not commit** — the router commits once the batch is fully processed so a parse failure rolls back the whole file.
- Returns rows skipped + error reasons rather than silently dropping bad rows.
- Defines a top-of-file `HEADER_MAP` that maps "what we call it" → "what TikTok calls it". Update the map when TikTok renames a column instead of touching parsing logic.

## What's stubbed

- Settlement, payout, SKU master, bundle mapping, and samples importers — interface ready, parsing not implemented.
- Bundle explosion in `reports/sku_profitability.py` — reports physical SKUs sold directly only; bundles need a follow-up pass.
- Alembic migrations directory.
- Auth — none. Internal app on a private network; add only if exposed.

## Things deliberately not built (yet)

Inventory forecasting, creator performance tracking, TikTok API integration, user permissions, multi-brand support. The data model is brand-aware (`Order.brand`, `Sku.brand`) so multi-brand is a query-filter change, not a schema change.
