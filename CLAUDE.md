# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Internal full-stack web app for managing the **Smashbox TikTok Shop P&L and operating reports**. Users (me and a small finance/ops team) upload TikTok Shop exports (orders, settlements, payouts) plus reference files (SKU master, bundle mapping, samples) and the app produces monthly P&L, YTD P&L, SKU-level profitability, sample tracking, and reconciliation against TikTok's raw numbers.

Private/internal. **Deployed to Fly.io** at https://smashbox.fly.dev/ (LAX region, single shared-cpu-1x VM, 1GB encrypted persistent volume at `/data`).

**Multi-tenancy (Phase 2a, schema-only).** Every tenant-scoped table has a nullable `shop_id` FK to `shops`. There is exactly one Shop today (`slug = "smashbox"`, `timezone = "America/Los_Angeles"`) — created by the boot migration in `app/main.py` (`_bootstrap_shop_and_backfill`), which also backfills `shop_id` on all existing rows and flips `User.is_super_admin = True` for any existing admins. **Queries are NOT yet scoped by `shop_id`** — that's Phase 2b. Adding multi-shop functionality therefore touches: (a) every report query (add `WHERE shop_id = current_user.shop_id`), (b) every importer (set `shop_id` on insert), (c) a new super-admin UI to create + switch shops. `Shop.timezone` is captured so daily reconciliation can render in the shop's local time and match TikTok's Seller Center display.

**Auth (Phase 1, per-user logins).** Replaces v1's HTTP Basic. Email + bcrypt-hashed password, server-side signed-cookie sessions via Starlette's `SessionMiddleware` (signed with the `SESSION_SECRET` Fly secret). `app/auth.py` houses both `hash_password`/`verify_password` (direct `bcrypt` calls — `passlib`'s wrapper is broken on bcrypt 4.x) and `SessionAuthMiddleware`. Forward-compat: `User.role` is `admin` or `member` with `admin` slated for the user-management UI; today every authenticated user can use every report. Bootstrap: on first boot, if no User rows exist and `INITIAL_ADMIN_EMAIL` + `INITIAL_ADMIN_PASSWORD` are set, that user is created as `admin`. Empty `SESSION_SECRET` disables auth entirely (local-dev convenience). Legacy `BasicAuthMiddleware` is still in `auth.py` as a safety net for environments that haven't set `SESSION_SECRET` yet — remove it once all deploys have migrated.

README has the operator playbook (deploy, rotate password, data migration, scaling). Production paths use env vars (`DATA_DIR=/data`, `UPLOAD_DIR=/data/uploads`, `EXPORT_DIR=/data/exports`) — repo defaults still point at repo-root paths for local dev.

**Known: 1-hour timezone offset vs TikTok Seller Center daily reporting.** Our `Order.placed_at` is the exact `Created Time` from the orders CSV. TikTok's Seller Center daily Sales tile buckets each order using a reporting timezone that's exactly 1 hour earlier than that timestamp (empirically determined 2026-05-19 against March 2026 data; sweeping offsets showed -1h collapses March's total absolute daily variance from $285.18 to $7.80). We DELIBERATELY do not shift stored timestamps — month totals are immaterial (well under 0.1% gap, sums to a few dollars), and a shift would risk breaking settlement reconciliation, P&L date filters, and policy-violation flagging that all key off `placed_at`. The Reconciliation page's daily-comparison block has an in-line explanation calling this out; amber-highlighted rows are informational, not bugs.

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

**Production runs on Fly Managed Postgres** (cluster `smashbox-db` / `zp2wjre645j0dn4q`, LAX) as of the SQLite→Postgres cutover (2026-06-12). The app connects through MPG's pgbouncer pooler via the `DATABASE_URL` Fly secret (`postgresql+psycopg://…@pgbouncer.<cluster>.flympg.net/fly-db`); `app/db.py` disables psycopg3 server-side prepared statements (`prepare_threshold=None`) and enables `pool_pre_ping` for pooler/idle-drop safety. **Local dev and the test suite still use SQLite** (no `DATABASE_URL`, or a temp file) — psycopg only loads for `postgresql://` URLs.

Schema is managed by **Alembic** (`alembic upgrade head`) — `alembic/versions/07c4ee33b7fa_baseline_schema.py` is the baseline (all 27 tables); `tests/test_migrations.py` guards models↔migrations parity. Boot still calls `Base.metadata.create_all` (idempotent, `checkfirst`) as a belt-and-suspenders for fresh DBs, but **new model columns must now go through an Alembic revision**, not the legacy `_ensure_columns` SQLite shim in `app/main.py` (that shim only fires for missing columns and is inert on the migrated Postgres). Rollback path: prod's pre-cutover SQLite file remains at `/data/smashbox.db` — unset the `DATABASE_URL` secret to revert.

## Restart uvicorn after editing Python — don't trust `--reload` alone

On Windows, `uvicorn app.main:app --reload` reliably picks up **template** changes (Jinja files are re-parsed on each request) but **frequently misses Python module changes** — especially when a dataclass shape changes, a new attribute is added, or a function signature shifts. The failure mode is silent and confusing: the page renders, but with stale code paths (e.g. the template's new `r.sku_code` attribute resolves to `undefined`, so its Jinja fallback fires and every row shows "Missing SKU" even though the function actually returns a populated value).

If you change anything in `app/`, kill the server and start it again. The cheapest pattern:

```bash
for pid in $(tasklist //FO CSV | grep -i python | cut -d',' -f2 | tr -d '"'); do
  taskkill //F //PID $pid 2>&1 | head -1
done
py -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload &
```

Whenever a user reports "the page shows the new columns/title but the data looks wrong (Missing / undefined / stale)", **restart first, debug second** — 9 times out of 10 it's a stale reload.

## Tests never touch the dev DB

`tests/conftest.py` sets `DATABASE_URL` to a temp SQLite file **before** any
`from app.db import ...` runs. This is load-bearing — multiple tests call
`Base.metadata.drop_all()` / `create_all()` to start clean, and a previous
version of this file allowed those calls to nuke `data/smashbox.db`. Don't
rearrange the conftest imports; the env-var-then-import order matters.

## Python 3.14 quirk

Many libs only ship 3.14 wheels in their latest minor versions, so `requirements.txt` uses lower-bound (`>=`) pins, not equality. `pydantic-core` will try to build from Rust source if you pin an older version — don't.

## Architecture

Layered, all under `app/`:

```
routers/       FastAPI route handlers — HTTP in, template/file out
templates/     Jinja2 (HTMX-ready) — base.html + partials/nav.html + per-page files
static/        Tailwind compiled → css/tailwind.css (gitignored); css/app.css holds only print rules
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

Implementation: compute Outlandish, quantize to cents; assign the residual to Smashbox so the sum is exact by construction. Don't "fix" a failing test by relaxing the invariant — fix the split function.

**Business rule (cap-then-residual, line-level, confirmed 2026-05-13):**

```
post_tiktok_price = gross_sales − platform_discount       (per line)
Outlandish        = MIN(seller_funded_discount, post_tiktok_price × 10%)
Smashbox          = seller_funded_discount − Outlandish
```

- The split is computed **at the OrderLine level** and rolled up to the Order. Line-level is the source of truth.
- `seller_funded_discount` is TikTok's `SKU Seller Discount` (the per-line value). NOT `SKU Platform Discount` — that one is TikTok-funded and never split.
- `post_tiktok_price` (the eligible base) is `SKU Subtotal Before Discount − SKU Platform Discount`. **NOT gross_sales** — the split is applied *after* TikTok-funded discounts reduce the customer price.
- `cap_pct` defaults to `OUTLANDISH_CAP_PCT` (0.10). Fixed today; can be made per-period/per-SKU later.

Canonical worked example (the one pinned by the user, 2026-05-13):

```
Gross Product Sales      = $100
TikTok-Funded Discount   = $20    →  post-TikTok price = $80
Total Seller-Funded Disc = $24
Outlandish Max (10%)     = $8     →  Outlandish = MIN($24, $8) = $8
Smashbox                  = $24 − $8 = $16
Validation: $8 + $16 = $24 ✓
```

**P&L presentation:** the discount waterfall is displayed line-by-line in the P&L so anyone reading it can see who funded what:

```
Gross Product Sales
− TikTok-Funded Discount
− Outlandish-Funded Discount
− Smashbox-Funded Discount
− Refunds
= Net Customer Sales (a.k.a. Net Product Revenue)
```

**Policy ceiling — uses a DIFFERENT base than the split:** total seller-funded discount per line must be `≤ 30%` of the line's **MSRP (gross_sales)** — NOT post-TikTok price. This decoupling is intentional: the split uses post-TikTok per the business rule, but the policy ceiling uses conventional discount-percentage language ("no SKU goes over 30% off retail"). Mixing the two bases would flag many lines as "violations" simply because TikTok ran a stacking platform promo.

Lines that breach the policy are still imported — Smashbox absorbs the excess so the exact-sum invariant holds — but they are flagged via `OrderLine.discount_policy_violation`, the order's flag is set too, the import logs a `policy:` warning, and reconciliation surfaces the count. Cap lives at `SELLER_FUNDED_POLICY_CAP_PCT` (0.30).

Use `violates_policy_cap(total, eligible_base, policy_cap_pct=None)` from `app/rules/seller_funded_split.py` — the importer calls this per-line **with `eligible_base=gross_sales`**, even though the splitter uses `post_tiktok_price`.

## Order taxonomy

`Order.order_type` (`OrderType` enum): `PAID` / `SAMPLE` / `PAID_SAMPLE`. The P&L includes `PAID` only. `SAMPLE` and `PAID_SAMPLE` feed the sample-tracking report. Free samples count against `FREE_SAMPLE_MONTHLY_ALLOWANCE`; anything over becomes paid oversampling.

**Sample-detection rule (confirmed 2026-05-13):** a TikTok orders row is a sample iff its order-level gross sales (`SKU Subtotal Before Discount` summed across lines) is `$0`. The TikTok orders importer applies this; the samples table is reserved for samples that did NOT ship through TikTok Shop. The sample-tracking report unions both sources.

`Order.unit_cogs_snapshot` is captured at import time so historical reports don't shift when the SKU master is edited later. If the snapshot is zero (legacy row) the report falls back to current `Sku.unit_cogs`.

## Settlement and adjustment imports are idempotent

`Settlement` has `UNIQUE (tiktok_order_id, linked_statement_id)`. The importer groups Orders-sheet rows by that pair before building each Settlement (sums money columns, takes non-money fields from the first row). Original per-line payloads are preserved under `Settlement.raw_payload['lines']`.

`Adjustment` has `UNIQUE (adjustment_id, adjustment_type, create_time)` — TikTok pairs balance/deduction rows under the same `adjustment_id`, so all three columns are needed to disambiguate.

Both importers upsert on those natural keys (query existing, update fields if found, insert otherwise), so re-uploading the same settlement file is a no-op: row counts and money totals stay identical.

## Settlement file is the financial source of truth

The TikTok orders export (`All order-*.csv`) tells us *what was ordered*. The settlement export (`merchant_statement_profit_loss_*.xlsx`, `Orders` sheet, header on row 5) tells us *what TikTok actually paid us and what the fees were*. The settlement importer **back-fills the matching Order row** with `tiktok_fees`, `affiliate_commission`, `shop_ads_cost`, `shipping_cost`, and `refunds` — so the P&L reads a single source (Order.*) instead of joining at query time.

When the settlement file is present, its `Sample order type` column overrides the gross_sales==0 sample heuristic — settlement is authoritative.

TikTok reports costs as NEGATIVE numbers and inflows as POSITIVE. We store costs as POSITIVE magnitudes on `Order.*` so the P&L renderer can subtract them directly. The `_pos` helper in the settlement importer enforces this.

### Quirks of the real exports (lock these in when adding more importers)

- TikTok orders CSV: `Order ID` and timestamps have a **trailing `\t`** — strip on ingest.
- Settlement workbook: header rows are NOT at the top. `Orders` sheet → header on row 5, `Adjustment` sheet → header on row 3.
- Settlement `Adjustment ID` is **not unique** — TikTok pairs `Net earnings balance` (+) and `Net earnings deduction` (−) under the same ID. No uniqueness constraint.
- The Adjustment sheet has a typo header: `llinked payout id` (double-`l`).
- Settlement dates are integers like `20260421` (YYYYMMDD), not ISO strings.

## Money

Always `Decimal`, never `float`. `Numeric(14, 2)` in the ORM; quantize to `0.01` when persisting. The `money` Jinja filter (`app/templating.py`) formats Decimals for display.

## Stack pointers

- FastAPI + Starlette 1.0 — **`TemplateResponse(request, "name.html", {...})`** — `request` is the first positional arg, NOT in the context dict. The old form silently turns the context into a Jinja cache key and crashes with "unhashable type: 'dict'".
- Jinja2 + HTMX (CDN).
- **Tailwind is compiled, NOT CDN.** `npm run css` builds `app/static/css/tailwind.css` from `tailwind.input.css`, scanning the templates listed in `tailwind.config.js`. That output is a **gitignored build artifact** — production rebuilds it in the Dockerfile's `node:24-slim` stage on every `fly deploy`, so prod can't ship stale CSS. **Local dev must run `npm run css` (or `npm run css:watch`) once before `uvicorn`, or every page renders unstyled** (the file 404s). Do NOT re-add `<script src="cdn.tailwindcss.com">` to `base.html`/`login.html` — that reverts this. Classes built dynamically by string interpolation (only `monthly_pnl.html`'s `bg-{{ sev }}-50` etc. today) are invisible to the scanner and survive only via the `safelist` in `tailwind.config.js` — extend it if you add more interpolated classes.
- **Icons are bundled Lucide, NOT a CDN or hand-rolled SVG.** Use `{{ ui.icon("name", "h-4 w-4 text-...") }}` (macro in `partials/ui.html`); it inlines a committed SVG via `app/icons.py` (`render_icon` strips the fixed size, keeps `currentColor`, so Tailwind `h-/w-`/`text-*` classes size + color it). To add one: copy `node_modules/lucide-static/icons/<name>.svg` (a devDep) into `app/static/icons/` and **commit it** (prod reads the committed copy via `COPY app`; no runtime JS). A guard test asserts every `ui.icon()` reference has a committed SVG. Don't hand-roll `<svg><path>` icons or add an icon CDN. (Deliberately still hand-rolled: the delta-chip ▲/▼ carets, the upload spinner, and the sparkline/bar-chart SVGs — those are bespoke, not icon glyphs.)
- SQLAlchemy 2.x with `Mapped[...]` annotations. `app/db.py` exports `engine`, `SessionLocal`, `Base`, `get_db`.
- pandas for parsing imports; xlsxwriter for Excel export.

## Catalog tables: Sku and Bundle

**Canonical product identifier is the TikTok SKU ID** (numeric string). It is the only key TikTok always emits, and it uniquely identifies a SKU/bundle. The orders importer prefers `SKU ID` (numeric, always present) over `Seller SKU` when building `OrderLine.sku`; the resolver canonicalizes any matched line to `Sku.tiktok_sku_id` (or `Bundle.tiktok_sku_id`).

`Sku` carries three identifiers — `sku` (SBX-form, human-readable code, **not** unique), `tiktok_alt_sku` (C-form), and `tiktok_sku_id` (canonical, unique when set). TikTok issues a separate `tiktok_sku_id` per variation, so one SBX-form code can map to multiple Sku rows — one per variation. The master-sheet importer therefore upserts by `tiktok_sku_id`, falling back to `sku` only for products without a TikTok ID yet. `Bundle` carries two — `bundle_sku` (synthesized SBX-form for display) and `tiktok_sku_id` (canonical).

The resolver at `app/services/sku_resolver.py` matches `OrderLine.sku` against any catalog key, then:
- rewrites `OrderLine.sku` to the canonical TikTok SKU ID,
- writes `unit_cogs_snapshot`: single SKU → `Sku.unit_cogs`; bundle → sum of component `quantity × unit_cogs` (`Bundle.calculated_cogs`).

Reports JOIN on `Sku.tiktok_sku_id == OrderLine.sku` (and the parallel for `Bundle`) and display the human-readable `Sku.name` + `Sku.sku` (SBX-form). Unmapped TikTok SKU IDs (no master row) show as **"Unmapped"** in the SKU profitability report so the gap is visible.

The resolver runs automatically after `TIKTOK_ORDERS`, `SKU_MASTER`, and `BUNDLE_MAPPING` imports, so loading the catalog later retroactively back-fills COGS on orders that came in first. Idempotent.

## Importers

`app/importers/__init__.py` has the `IMPORTERS: dict[ImportFileKind, type[BaseImporter]]` registry. Eleven wired up today: `TIKTOK_ORDERS`, `TIKTOK_SETTLEMENTS`, `TIKTOK_PAYOUTS`, `TIKTOK_ADS`, `TIKTOK_ANALYTICS`, `TIKTOK_GMV_MAX`, `SKU_MASTER`, `BUNDLE_MAPPING`, `SAMPLES`, `INVENTORY_SNAPSHOT`, `SUPPLIER_RECEIPTS`. (The old "payouts/samples still TODO" note is obsolete — both are built; the payouts importer parses the `payouts-income_*.xlsx` Payments + Statements sheets and feeds the per-payout cash reconciliation in `app/reports/reconciliation.py`.) Each importer:

- Subclasses `BaseImporter`, implements `run(path, db, batch) -> ImportResult`.
- **Does not commit** — the router commits once the batch is fully processed so a parse failure rolls back the whole file.
- Returns rows skipped + error reasons rather than silently dropping bad rows.
- Defines a top-of-file `HEADER_MAP` that maps "what we call it" → "what TikTok calls it". Update the map when TikTok renames a column instead of touching parsing logic.

## What's stubbed

- `schemas/` (Pydantic) and `services/` are mostly empty — add as APIs/orchestration stabilize.

## Things deliberately not built (yet)

Inventory forecasting, creator performance tracking, TikTok API integration, user permissions, multi-brand support. The data model is brand-aware (`Order.brand`, `Sku.brand`) so multi-brand is a query-filter change, not a schema change.
