# Email the Sales & Sample Reports — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-report "Email report (HTML + CSV)" — a manual button (on-screen scope) + a recurring schedule (configurable rolling window) — to the Sales (`/reports/sales`) and Sample (`/reports/samples`) reports, mirroring the inventory email feature. The HTML body and CSV in each email are built from one rows source so they always match.

**Architecture:** A shared `report_email_common` (email styles + rolling-period resolver + scheduler-registration helper), 12 new `Shop` columns (one migration), two `*_report_email.py` services, routes + scheduler jobs mirroring inventory, and extracted CSV builders. Spec: `docs/superpowers/specs/2026-06-23-report-email-sales-samples-design.md`.

**Tech Stack:** FastAPI, SQLAlchemy 2.x, Alembic, APScheduler, Jinja2, smtplib (`app/services/mailer.py`), pytest. Branch: `feature/report-email-sales-samples`.

**Conventions:**
- Tests via Bash: `py -m pytest <path> -v 2>&1 | tail -25`.
- Commit: write `.git/COMMIT_MSG_DRAFT.txt` with the **Write tool** (NOT printf — `%` breaks it), then `git commit -F`. Co-author trailer: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- New `Shop` columns MUST go through an Alembic revision (Postgres cutover).
- `mailer.send_email(subject, body, *, to, html, attachments=[(filename, bytes, subtype)])`; attachments are added as `application/<subtype>` — pass `"csv"` (→ `application/csv`, which mail clients open as a spreadsheet).
- Reference implementation to mirror: inventory — `app/services/inventory_report_email.py`, the three routes at `app/routers/reports.py:343-413`, the scheduler funcs `app/services/scheduler.py` (`_run_inventory_report_job`, `_alert_report_failure`, `apply_inventory_report_schedule`, `_primary_shop`, `REPORT_JOB_ID`), and the settings card in `app/templates/reports/inventory_report.html` (~lines 33-85).

---

## Task 1: Shared `report_email_common`

**Files:** Create `app/services/report_email_common.py`; Test `tests/test_report_rolling_period.py`.

- [ ] **Step 1: Write the failing tests** — `tests/test_report_rolling_period.py`:

```python
# tests/test_report_rolling_period.py
"""Rolling-window resolver for scheduled report emails — deterministic via a
fixed `today`."""
from datetime import date

from app.services.report_email_common import (
    SALES_PERIODS, SAMPLE_PERIODS, resolve_rolling_period,
)

TODAY = date(2026, 6, 15)   # day <= 28 → current fiscal month == (2026, 6)


def test_prev_month():
    w = resolve_rolling_period("prev_month", today=TODAY)
    assert (w.start, w.end) == (date(2026, 5, 1), date(2026, 5, 31))
    assert w.fiscal_ym is None


def test_mtd():
    w = resolve_rolling_period("mtd", today=TODAY)
    assert (w.start, w.end) == (date(2026, 6, 1), date(2026, 6, 15))


def test_last_7_and_30():
    assert resolve_rolling_period("last_7", today=TODAY).start == date(2026, 6, 9)
    assert resolve_rolling_period("last_7", today=TODAY).end == date(2026, 6, 15)
    assert resolve_rolling_period("last_30", today=TODAY).start == date(2026, 5, 17)


def test_prev_week_is_a_mon_sun_block_before_this_week():
    w = resolve_rolling_period("prev_week", today=TODAY)
    assert w.start.weekday() == 0 and w.end.weekday() == 6   # Mon..Sun
    assert w.end == w.start.replace() and (w.end - w.start).days == 6
    this_monday = date(2026, 6, 15) - __import__("datetime").timedelta(days=TODAY.weekday())
    assert w.end < this_monday                               # strictly last week


def test_prev_fiscal_month():
    w = resolve_rolling_period("prev_fiscal_month", today=TODAY)
    # current fiscal (2026,6) → previous fiscal (2026,5) = 29 Apr .. 28 May
    assert w.fiscal_ym == (2026, 5)
    assert (w.start, w.end) == (date(2026, 4, 29), date(2026, 5, 28))


def test_unknown_key_falls_back_to_prev_month():
    w = resolve_rolling_period("bogus", today=TODAY)
    assert (w.start, w.end) == (date(2026, 5, 1), date(2026, 5, 31))


def test_period_allowlists():
    assert "prev_fiscal_month" in SALES_PERIODS
    assert "prev_fiscal_month" not in SAMPLE_PERIODS    # samples is month-granular
    assert SAMPLE_PERIODS == ["prev_month", "mtd"]
```

- [ ] **Step 2: Run to verify it fails** — `py -m pytest tests/test_report_rolling_period.py -v 2>&1 | tail -20`. Expected: `No module named 'app.services.report_email_common'`.

- [ ] **Step 3: Create the module** — `app/services/report_email_common.py`:

```python
# app/services/report_email_common.py
"""Shared pieces for the per-report email features (Sales, Samples): inline email
CSS, the rolling-period resolver for scheduled sends, and a generic APScheduler
registration helper. Inventory keeps its own copy of the styles + its own scheduler
function (left untouched — it is in prod)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

# Inline styles approximating the dashboard (email clients strip CSS classes).
CARD = ("border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;font-family:"
        "-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:760px")
HEADER = "padding:16px 12px;background:#f8fafc"
H_TITLE = "font-size:16px;font-weight:700;color:#0f172a"
H_SUB = "font-size:12px;color:#475569;margin-top:2px"
TH = ("padding:8px 12px;text-align:left;font-size:10px;font-weight:600;text-transform:"
      "uppercase;letter-spacing:.05em;color:#64748b;border-bottom:1px solid #e2e8f0")
TH_R = TH + ";text-align:right"
TD = "padding:8px 12px;font-size:13px;color:#0f172a;border-bottom:1px solid #f1f5f9"
TD_R = TD + ";text-align:right;font-variant-numeric:tabular-nums"
TOT = "padding:8px 12px;font-size:13px;font-weight:700;color:#0f172a;border-top:2px solid #e2e8f0"
TOT_R = TOT + ";text-align:right;font-variant-numeric:tabular-nums"

# (key → label). The per-report allow-lists pick which keys each report offers.
ROLLING_PERIODS = {
    "prev_month": "Previous month",
    "mtd": "Month-to-date",
    "prev_week": "Previous week (Mon–Sun)",
    "last_7": "Last 7 days",
    "last_30": "Last 30 days",
    "prev_fiscal_month": "Previous fiscal month",
}
SALES_PERIODS = ["prev_month", "mtd", "prev_week", "last_7", "last_30", "prev_fiscal_month"]
SAMPLE_PERIODS = ["prev_month", "mtd"]   # month-granular report → month-level only


@dataclass(frozen=True)
class RollingWindow:
    start: date                       # inclusive (calendar)
    end: date                         # inclusive
    label: str
    fiscal_ym: tuple[int, int] | None = None   # set only for prev_fiscal_month


def _first_of_month(d: date) -> date:
    return d.replace(day=1)


def _last_of_month(d: date) -> date:
    nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return nxt - timedelta(days=1)


def resolve_rolling_period(key: str, *, today: date) -> RollingWindow:
    """Recompute the concrete inclusive [start, end] for a rolling-window key,
    relative to `today` (shop-local). Unknown key → prev_month."""
    label = ROLLING_PERIODS.get(key, ROLLING_PERIODS["prev_month"])
    if key == "mtd":
        return RollingWindow(_first_of_month(today), today, label)
    if key == "last_7":
        return RollingWindow(today - timedelta(days=6), today, label)
    if key == "last_30":
        return RollingWindow(today - timedelta(days=29), today, label)
    if key == "prev_week":
        this_monday = today - timedelta(days=today.weekday())
        last_monday = this_monday - timedelta(days=7)
        return RollingWindow(last_monday, last_monday + timedelta(days=6), label)
    if key == "prev_fiscal_month":
        from app.reports.sales_report import current_fiscal_ym
        fy, fm = current_fiscal_ym(today)                 # current fiscal month
        pfy, pfm = (fy, fm - 1) if fm > 1 else (fy - 1, 12)
        end = date(pfy, pfm, 28)                          # fiscal month closes on the 28th
        start = date(pfy - 1, 12, 29) if pfm == 1 else date(pfy, pfm - 1, 29)
        return RollingWindow(start, end, label, fiscal_ym=(pfy, pfm))
    # prev_month (default)
    last_prev = _first_of_month(today) - timedelta(days=1)
    return RollingWindow(_first_of_month(last_prev), last_prev, label)


def register_report_job(scheduler, job_id, *, enabled, recipients, days, hour,
                        minute, timezone, run_fn) -> None:
    """Add/replace or remove a report-email cron job to match config. No-op when
    the scheduler isn't running. Registered only when enabled AND recipients exist."""
    if scheduler is None:
        return
    if not (enabled and recipients):
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
        return
    from apscheduler.triggers.cron import CronTrigger
    scheduler.add_job(
        run_fn,
        trigger=CronTrigger(day_of_week=days, hour=hour, minute=minute, timezone=timezone),
        id=job_id, replace_existing=True, coalesce=True,
        misfire_grace_time=3600, max_instances=1,
    )
```

- [ ] **Step 4: Run the tests** — `py -m pytest tests/test_report_rolling_period.py -v 2>&1 | tail -20`. Expected: 7 passed.

- [ ] **Step 5: Commit** — message `report email: shared rolling-period + scheduler helper` (Write tool → `.git/COMMIT_MSG_DRAFT.txt`, with co-author trailer). `git add app/services/report_email_common.py tests/test_report_rolling_period.py && git commit -F .git/COMMIT_MSG_DRAFT.txt`.

---

## Task 2: `Shop` columns + Alembic migration

**Files:** Modify `app/models/shop.py`; Create `alembic/versions/<rev>_report_email_columns.py`.

- [ ] **Step 1: Add the columns + properties** — in `app/models/shop.py`, after the inventory-report block, add 12 columns and 2 properties (mirror the inventory shape; add `period`):

```python
    # ---- Sales-report email (managed on /reports/sales) ----------------------
    sales_report_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    sales_report_hour: Mapped[int] = mapped_column(Integer, default=8)
    sales_report_minute: Mapped[int] = mapped_column(Integer, default=0)
    sales_report_days: Mapped[str] = mapped_column(String(64), default="mon")
    sales_report_recipients: Mapped[str] = mapped_column(String(1024), default="")
    sales_report_period: Mapped[str] = mapped_column(String(32), default="prev_month")

    # ---- Sample-report email (managed on /reports/samples) -------------------
    sample_report_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    sample_report_hour: Mapped[int] = mapped_column(Integer, default=8)
    sample_report_minute: Mapped[int] = mapped_column(Integer, default=0)
    sample_report_days: Mapped[str] = mapped_column(String(64), default="mon")
    sample_report_recipients: Mapped[str] = mapped_column(String(1024), default="")
    sample_report_period: Mapped[str] = mapped_column(String(32), default="prev_month")

    @property
    def sales_report_recipients_list(self) -> list[str]:
        return [a.strip() for a in (self.sales_report_recipients or "").split(",") if a.strip()]

    @property
    def sample_report_recipients_list(self) -> list[str]:
        return [a.strip() for a in (self.sample_report_recipients or "").split(",") if a.strip()]
```

- [ ] **Step 2: Create the migration** — find the current head: `python -m alembic heads 2>&1 | tail -3` (or look at the latest `alembic/versions/*.py`'s `revision`). Create `alembic/versions/<newrev>_report_email_columns.py` with `down_revision = "<current head>"`:

```python
"""report email columns on shops

Revision ID: <newrev>
Revises: <current head>
"""
import sqlalchemy as sa
from alembic import op

revision = "<newrev>"
down_revision = "<current head>"
branch_labels = None
depends_on = None

_COLS = [
    ("sales_report_enabled", sa.Boolean(), sa.false()),
    ("sales_report_hour", sa.Integer(), sa.text("8")),
    ("sales_report_minute", sa.Integer(), sa.text("0")),
    ("sales_report_days", sa.String(length=64), sa.text("'mon'")),
    ("sales_report_recipients", sa.String(length=1024), sa.text("''")),
    ("sales_report_period", sa.String(length=32), sa.text("'prev_month'")),
    ("sample_report_enabled", sa.Boolean(), sa.false()),
    ("sample_report_hour", sa.Integer(), sa.text("8")),
    ("sample_report_minute", sa.Integer(), sa.text("0")),
    ("sample_report_days", sa.String(length=64), sa.text("'mon'")),
    ("sample_report_recipients", sa.String(length=1024), sa.text("''")),
    ("sample_report_period", sa.String(length=32), sa.text("'prev_month'")),
]


def upgrade() -> None:
    for name, type_, default in _COLS:
        op.add_column("shops", sa.Column(name, type_, nullable=False, server_default=default))
    # Drop the server_defaults so the live schema matches the model (Python-side
    # defaults only), keeping the existing row backfilled.
    for name, _type, _default in _COLS:
        op.alter_column("shops", name, server_default=None)


def downgrade() -> None:
    for name, _type, _default in reversed(_COLS):
        op.drop_column("shops", name)
```
(The `alter_column … server_default=None` is a no-op on SQLite's batch limitations but harmless; if `alembic upgrade head` errors on SQLite for the alter, wrap the drop-default loop so it's skipped on SQLite — but typically Alembic handles add+alter on SQLite via batch. If the parity test passes without dropping defaults, you may omit the second loop.)

- [ ] **Step 3: Apply + verify parity** — run:
```
python -m alembic upgrade head 2>&1 | tail -5
py -m pytest tests/test_migrations.py -v 2>&1 | tail -20
```
Expected: upgrade succeeds; the models↔migrations parity test passes (the 12 new columns now exist in both). If parity flags a server_default mismatch, keep the `alter_column … server_default=None` loop (it makes the DB schema match the model). Fix until green; do NOT weaken the parity test.

- [ ] **Step 4: Commit** — `report email: shops columns + migration`. `git add app/models/shop.py alembic/versions/<newrev>_report_email_columns.py && git commit -F .git/COMMIT_MSG_DRAFT.txt`.

---

## Task 3: Sales report email (end-to-end)

**Files:** Modify `app/routers/reports.py` (extract CSV + 2 routes + page context); Create `app/services/sales_report_email.py`; Modify `app/services/scheduler.py`; Create `app/templates/partials/report_email_settings.html` + include in `app/templates/reports/sales.html`; Tests `tests/test_sales_report_email.py`, `tests/test_sales_email_routes.py`.

- [ ] **Step 1: Write the failing service tests** — `tests/test_sales_report_email.py`:

```python
# tests/test_sales_report_email.py
"""Sales report email: render (HTML matches CSV), CSV builder, and send via a
fake mailer."""
import itertools
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.order import Order, OrderLine, OrderType
from app.reports.sales_report import compute_sales_report
from app.services import sales_report_email as sre


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine); Base.metadata.create_all(bind=engine); yield


_OID = itertools.count(1)


def _order(db, d, rev, units):
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                    original_filename="t", stored_path="t")
    db.add(b); db.flush()
    o = Order(import_batch_id=b.id, tiktok_order_id=f"O{next(_OID)}",
              placed_at=datetime(d.year, d.month, d.day, 12, 0), order_type=OrderType.PAID,
              status="Completed", brand="smashbox", gross_sales=Decimal(str(rev)),
              shipping_revenue=Decimal("0"), seller_funded_outlandish=Decimal("0"),
              seller_funded_smashbox=Decimal("0"), platform_discount_total=Decimal("0"),
              payment_platform_discount=Decimal("0"))
    db.add(o); db.flush()
    db.add(OrderLine(order_id=o.id, sku="S1", quantity=units, gross_sales=Decimal(str(rev))))
    db.flush()


def _view(db):
    return compute_sales_report(db, "daily", start=date(2026, 5, 1), end=date(2026, 5, 31))


def test_render_and_csv_share_rows():
    with SessionLocal() as db:
        _order(db, date(2026, 5, 10), 100, 3)
        _order(db, date(2026, 5, 12), 50, 2); db.commit()
        view = _view(db)
    subject, html, text = sre.render_sales_email(view, window_label="May 2026")
    csv = sre.build_sales_csv(view).decode()
    assert "May 2026" in subject
    # HTML↔CSV parity: every non-zero bucket's revenue appears in BOTH renderings.
    assert "100.00" in html and "100.00" in csv
    assert "50.00" in html and "50.00" in csv
    # CSV header matches the on-screen export columns.
    assert csv.splitlines()[0] == "Period,Start,Revenue,Units,Orders,AOV,In Progress"


def test_send_sales_report_uses_mailer(monkeypatch):
    calls = {}
    def fake_send(subject, body, *, to, html=None, attachments=None):
        calls.update(subject=subject, to=to, html=html, attachments=attachments)
    monkeypatch.setattr(sre.mailer, "send_email", fake_send)
    with SessionLocal() as db:
        _order(db, date(2026, 5, 10), 100, 3); db.commit()
        sre.send_sales_report(db, recipients=["a@x.com"], granularity="daily",
                              start_date="2026-05-01", end_date="2026-05-31",
                              year=None, month=None)
    assert calls["to"] == ["a@x.com"]
    assert calls["html"] and len(calls["attachments"]) == 1
    assert calls["attachments"][0][0].endswith(".csv")
    assert calls["attachments"][0][2] == "csv"


def test_send_requires_recipients():
    with SessionLocal() as db, pytest.raises(ValueError):
        sre.send_sales_report(db, recipients=[], granularity="daily",
                              start_date=None, end_date=None, year=None, month=None)
```

- [ ] **Step 2: Run to verify failure** — `py -m pytest tests/test_sales_report_email.py -v 2>&1 | tail -20`. Expected: import error (`sales_report_email` missing).

- [ ] **Step 3: Create the service** — `app/services/sales_report_email.py`. Use the `SalesReportView`/`SalesBucket` fields (`view.buckets` each with `.label, .start, .revenue, .units, .orders, .aov, .in_progress`; totals `view.total_revenue/total_units/total_orders/avg_aov/avg_daily_revenue`; `view.window_start/window_end`). Build the HTML table + CSV from `view.buckets` (same rows), KPIs from `view` totals:

```python
# app/services/sales_report_email.py
"""Email the Sales velocity report: an inline-styled HTML summary (KPIs + the
velocity table) and the velocity CSV — both built from the same view.buckets, so
the HTML and the attachment always match. Thin send seam over mailer.send_email."""
from __future__ import annotations

import csv
import io
from html import escape

from sqlalchemy.orm import Session

from app.services import mailer
from app.services.report_email_common import (
    CARD, HEADER, H_TITLE, H_SUB, TH, TH_R, TD, TD_R, TOT, TOT_R,
)

_HEADERS = ["Period", "Start", "Revenue", "Units", "Orders", "AOV", "In Progress"]


def _csv_rows(view):
    for b in view.buckets:
        yield [b.label, b.start.isoformat(), f"{b.revenue:.2f}", b.units, b.orders,
               f"{b.aov:.2f}", "yes" if b.in_progress else ""]


def build_sales_csv(view) -> bytes:
    """The sales velocity CSV — identical columns to /reports/sales.csv."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_HEADERS)
    for row in _csv_rows(view):
        w.writerow(row)
    return buf.getvalue().encode()


def render_sales_email(view, *, window_label: str):
    """(subject, html, text). HTML KPIs + velocity table from the same view."""
    subject = f"Smashbox Sales Report — {window_label}"
    kpis = (
        f'<div style="{H_SUB}">Revenue {view.total_revenue:,.2f} · '
        f'Units {view.total_units} · Orders {view.total_orders} · '
        f'AOV {view.avg_aov:,.2f} · Avg/day {view.avg_daily_revenue:,.2f}</div>'
    )
    rows = []
    for b in view.buckets:
        rows.append(
            f'<tr><td style="{TD}">{escape(b.label)}</td>'
            f'<td style="{TD}">{b.start.isoformat()}</td>'
            f'<td style="{TD_R}">{b.revenue:,.2f}</td>'
            f'<td style="{TD_R}">{b.units}</td>'
            f'<td style="{TD_R}">{b.orders}</td>'
            f'<td style="{TD_R}">{b.aov:,.2f}</td>'
            f'<td style="{TD}">{"in progress" if b.in_progress else ""}</td></tr>'
        )
    total = (
        f'<tr><td style="{TOT}" colspan="2">Total</td>'
        f'<td style="{TOT_R}">{view.total_revenue:,.2f}</td>'
        f'<td style="{TOT_R}">{view.total_units}</td>'
        f'<td style="{TOT_R}">{view.total_orders}</td>'
        f'<td style="{TOT_R}">{view.avg_aov:,.2f}</td><td style="{TOT}"></td></tr>'
    )
    html = (
        f'<div style="{CARD}"><div style="{HEADER}">'
        f'<div style="{H_TITLE}">Smashbox Sales Report</div>'
        f'<div style="{H_SUB}">{escape(window_label)}</div>{kpis}</div>'
        f'<table style="width:100%;border-collapse:collapse"><thead><tr>'
        f'<th style="{TH}">Period</th><th style="{TH}">Start</th>'
        f'<th style="{TH_R}">Revenue</th><th style="{TH_R}">Units</th>'
        f'<th style="{TH_R}">Orders</th><th style="{TH_R}">AOV</th>'
        f'<th style="{TH}">Status</th></tr></thead>'
        f'<tbody>{"".join(rows)}{total}</tbody></table></div>'
    )
    text_lines = [f"Smashbox Sales Report — {window_label}",
                  f"Revenue {view.total_revenue:,.2f} · Units {view.total_units} · "
                  f"Orders {view.total_orders} · AOV {view.avg_aov:,.2f}", ""]
    for b in view.buckets:
        text_lines.append(f"{b.label:<14}{b.revenue:>12,.2f}{b.units:>7}{b.orders:>7}")
    return subject, html, "\n".join(text_lines)


def send_sales_report(db: Session, *, recipients: list[str], granularity: str,
                      start_date, end_date, year, month) -> None:
    """Compute the sales view for the given scope, render, attach the CSV, send."""
    if not recipients:
        raise ValueError("no recipients configured for the sales report")
    # Local import avoids a router import cycle.
    from app.routers.reports import _sales_view_data
    ctx = _sales_view_data(db, granularity, start_date, end_date, year, month)
    view = ctx["view"]
    window_label = f"{view.window_start:%b %d} – {view.window_end:%b %d, %Y}"
    subject, html, text = render_sales_email(view, window_label=window_label)
    csv_bytes = build_sales_csv(view)
    fname = f"smashbox_sales_{view.window_start:%Y%m%d}_{view.window_end:%Y%m%d}.csv"
    mailer.send_email(subject, text, to=recipients, html=html,
                      attachments=[(fname, csv_bytes, "csv")])
```
Then **refactor the `sales_csv` route** (`app/routers/reports.py:618`) to reuse `build_sales_csv`: import it, replace the inline `rows()`/`_csv_response(...)` with
`return Response(content=build_sales_csv(view), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="sales_{suffix}.csv"'})` — keeping the existing `suffix` logic. (Confirms one source for the CSV.)

- [ ] **Step 4: Run service tests** — `py -m pytest tests/test_sales_report_email.py -v 2>&1 | tail -20` → 3 passed. Also `py -m pytest tests/test_sales_page.py -q 2>&1 | tail -5` (the refactored CSV route still works).

- [ ] **Step 5: Add the routes + page context** — in `app/routers/reports.py`, mirror the inventory trio (`:343-413`). Import `send_sales_report` + `apply_sales_report_schedule` + the period allow-list. Add:
  - In `sales_view`, fetch `shop = db.query(Shop).order_by(Shop.id).first()` and add to ctx: `"shop": shop`, `"valid_days": _REPORT_VALID_DAYS`, `"sales_periods": [(k, ROLLING_PERIODS[k]) for k in SALES_PERIODS]`, `"smtp_configured": bool(settings.smtp_host)`, and read `sent`/`err` query params for the flash. (Add `sent: str | None = None, err: str | None = None` to `sales_view`'s signature.)
  - `POST /reports/sales/email-settings` (dependencies `[Depends(require_admin)]`): Form fields `recipients`, `report_time="08:00"`, `enabled=None`, `period="prev_month"`, `days=[]`. Parse hour/minute from `report_time` (like inventory); `chosen = [d for d in _REPORT_VALID_DAYS if d in set(days)]`; `clean = [...]`; `period = period if period in SALES_PERIODS else "prev_month"`. Persist `shop.sales_report_*`, set `enabled = bool(enabled is not None and chosen and clean)`, `db.commit()`, `apply_sales_report_schedule(shop)`, redirect `/reports/sales?sent=settings` (or back). 
  - `POST /reports/sales/send-now` (admin): Form fields = the current on-screen scope (`granularity, start_date, end_date, year, month`). Get shop; if no `sales_report_recipients_list` → redirect `?err=no-recipients`. `try: send_sales_report(db, recipients=shop.sales_report_recipients_list, granularity=…, start_date=…, end_date=…, year=…, month=…)` `except Exception: ?err=send-failed`. Success → `?sent=ok`.

- [ ] **Step 6: Add the scheduler job** — in `app/services/scheduler.py`, mirror inventory. Add `SALES_REPORT_JOB_ID = "sales_report_email"`; `_run_sales_report_job()` (own session via `SessionLocal`, `shop = _primary_shop(db)`, bail if disabled/no recipients, `from app.services.report_email_common import resolve_rolling_period`, `from app.services.scheduler import ...`; resolve `shop.sales_report_period` to a window; if `window.fiscal_ym`: call `send_sales_report(db, recipients=…, granularity="fiscal_month", start_date=None, end_date=None, year=window.fiscal_ym[0], month=window.fiscal_ym[1])`; else `send_sales_report(db, recipients=…, granularity="daily", start_date=window.start.isoformat(), end_date=window.end.isoformat(), year=None, month=None)`; on exception log + `_alert_report_failure()`); and `apply_sales_report_schedule(shop)` = a thin wrapper calling `register_report_job(_scheduler, SALES_REPORT_JOB_ID, enabled=shop.sales_report_enabled, recipients=shop.sales_report_recipients_list, days=shop.sales_report_days, hour=shop.sales_report_hour, minute=shop.sales_report_minute, timezone=shop.timezone, run_fn=_run_sales_report_job)`. Register it at boot wherever `apply_inventory_report_schedule` is called for the primary shop.

- [ ] **Step 7: Template** — create `app/templates/partials/report_email_settings.html` (a macro `card(action_prefix, shop_attr, recipients, enabled, days_csv, hour, minute, period, periods, valid_days, scope_fields, smtp_configured, flash_sent, flash_err)`) by **copying the inventory settings card** (`inventory_report.html` ~33-85) and generalizing: parameterize the form `action` (`{{ action_prefix }}/email-settings`, `{{ action_prefix }}/send-now`), add a **period `<select name="period">`** populated from `periods` (the `(key,label)` list) with the current `period` selected, and render `scope_fields` (caller-supplied hidden inputs for the send-now scope). Include it on `app/templates/reports/sales.html` (Overview tab, near the page header), passing the sales scope as hidden fields (`granularity`, `start_date`, `end_date`, `year`, `month`) and the `?sent`/`?err` flash. Run `npm run css` if any new classes are introduced (prefer reusing inventory's existing classes so none are).

- [ ] **Step 8: Route/render tests** — `tests/test_sales_email_routes.py`: `POST /reports/sales/email-settings` persists recipients/period/days and flips `enabled`; invalid period → `prev_month`; `POST /reports/sales/send-now` with recipients (monkeypatch `app.services.sales_report_email.mailer.send_email`) → `?sent=ok` and mailer called; no recipients → `?err=no-recipients`; the settings card renders on `/reports/sales`. Use the admin-auth test pattern from the existing inventory route tests (find `tests/` for the inventory email route test and mirror its auth setup).

- [ ] **Step 9: Run + commit** — `py -m pytest tests/test_sales_report_email.py tests/test_sales_email_routes.py tests/test_report_rolling_period.py -q 2>&1 | tail -8` green; regression `py -m pytest tests/test_sales_page.py tests/test_sales_skus_tab.py -q 2>&1 | tail -5`. Commit `report email: sales report (button + schedule + HTML/CSV)`.

---

## Task 4: Sample report email (end-to-end)

**Files:** Modify `app/routers/exports.py` (extract `build_sample_csv` + refactor `/samples-by-sku.csv`); Modify `app/routers/reports.py` (2 routes + `samples_view` context); Create `app/services/sample_report_email.py`; Modify `app/services/scheduler.py`; reuse the `report_email_settings.html` partial on `app/templates/reports/sample_tracking.html`; Tests `tests/test_sample_report_email.py`, `tests/test_sample_email_routes.py`.

Mirror Task 3 exactly, with these specifics:
- **Dataset = `samples_by_sku_shipped(db, start, end)`** → `ShippedSamplesBySkuRow` (`sku_code`, `name`, `tiktok_sku_id`, `samples_sent`, `sample_orders_shipped`, `units_sold`, `sold_per_sample`). The HTML table AND the CSV render these same rows; totals are summed from them.
- `build_sample_csv(rows) -> bytes` — same columns/format as `/samples-by-sku.csv` (header `sku_code,name,tiktok_sku_id,samples_sent,sample_orders_shipped,units_sold,sold_per_sample`). Refactor `export_samples_by_sku_csv` (`app/routers/exports.py:323`) to build its body via this function.
- `render_sample_email(rows, *, title_suffix) -> (subject, html, text)` — header + a totals line (sum of `samples_sent`, `sample_orders_shipped`, `units_sold`) + the by-SKU table (SKU · Product · Samples Sent · Orders Shipped · Units Sold · Sold/Sample).
- `send_sample_report(db, *, recipients, period, year, month, start_year, start_month, end_year, end_month)` — resolve the window via the existing CSV path (`compute_pnl_view(db, PeriodKind, …)` + `window_for`), pull `rows = samples_by_sku_shipped(db, start, end)` once, render + attach CSV (both from `rows`), send. The `title_suffix` comes from the pnl view (so it matches the page). Empty recipients → `ValueError`.
- Routes: `POST /reports/samples/email-settings` (period allow-list = `SAMPLE_PERIODS`) + `POST /reports/samples/send-now` (scope fields = `period, year, month, start_year, start_month, end_year, end_month` — the SamplePeriodKind scope). `samples_view` gains `shop`/`valid_days`/`sample_periods`/`smtp_configured`/`sent`/`err` context + the settings card include.
- Scheduler: `SAMPLE_REPORT_JOB_ID = "sample_report_email"`; `_run_sample_report_job()` resolves `shop.sample_report_period` (only `prev_month`/`mtd`, both calendar-month windows) → `(year, month)` from `window.start` → `send_sample_report(db, recipients=…, period=PeriodKind.MONTH, year=window.start.year, month=window.start.month, start_year=None, start_month=None, end_year=None, end_month=None)`; `apply_sample_report_schedule(shop)` via `register_report_job`. Register at boot.
- Tests mirror Task 3, incl. the **HTML↔CSV parity** test (every SKU row + the totals appear in both the HTML and the CSV) and the fake-mailer send test (one `.csv` attachment).

- [ ] Build it in the same TDD step order as Task 3 (failing service tests → service + CSV extract → service tests green → routes → scheduler → template include → route tests → commit `report email: sample report (button + schedule + HTML/CSV)`).

---

## Task 5: Full suite + deploy + verify

- [ ] **Step 1:** `py -m pytest 2>&1 | tail -12` → all pass (905 + the new email/rolling/route tests).
- [ ] **Step 2: Merge + deploy (local-merge, no PR):**
```bash
git push -u origin feature/report-email-sales-samples
git checkout main && git pull --ff-only
git merge --no-ff feature/report-email-sales-samples -m "Merge feature/report-email-sales-samples"
git push origin main
git branch -d feature/report-email-sales-samples && git push origin --delete feature/report-email-sales-samples
fly deploy
```
The release command runs `alembic upgrade head` — the new migration applies on prod Postgres before the version goes live (a failure aborts the deploy).
- [ ] **Step 3: Verify** — `fly releases` healthy; `curl … /healthz` → 200; load `/reports/sales` + `/reports/samples` (302→login expected). Then ask the user to: set recipients on each page, hit "Email report" (manual, on-screen scope), confirm the email arrives with the HTML body AND the matching CSV; set a schedule + rolling period and confirm it saves. Defaults are off + empty, so nothing sends until configured.

---

## Self-Review

**Spec coverage:** shared `report_email_common` (styles + resolver + register helper) — Task 1 ✓; 12 Shop columns + migration + per-report `recipients_list` — Task 2 ✓; per-report manual button (on-screen scope) + recurring schedule (rolling period) — Tasks 3/4 ✓; **HTML↔CSV built from one dataset** (sales=`view.buckets`, samples=`samples_by_sku_shipped`) + parity tests — Tasks 3/4 ✓; extracted CSV builders shared with the existing routes — Tasks 3/4 ✓; scheduler jobs + sync-failure alert on failure — Tasks 3/4 ✓; separate recipients per report ✓; failure/empty-recipient handling ✓; fiscal rolling for sales only (samples month-level) ✓.

**Placeholder scan:** the migration `<rev>`/`<current head>` are intentionally to-be-filled at execution (alembic head is environment-specific); everything else is concrete. Route/scheduler/template steps reference the exact inventory lines to mirror with the precise adaptations.

**Type consistency:** `send_sales_report(db, *, recipients, granularity, start_date, end_date, year, month)` matches the route + scheduler callers; `send_sample_report(...)` params match the SamplePeriodKind scope; `resolve_rolling_period(key, *, today) -> RollingWindow(start,end,label,fiscal_ym)` matches the scheduler usage; `register_report_job(scheduler, job_id, *, enabled, recipients, days, hour, minute, timezone, run_fn)` matches both `apply_*` wrappers; `build_sales_csv(view)`/`build_sample_csv(rows)` shared by route + email. Shop attrs (`sales_report_*`, `sample_report_*`, `*_recipients_list`) consistent across model/routes/scheduler/templates.
