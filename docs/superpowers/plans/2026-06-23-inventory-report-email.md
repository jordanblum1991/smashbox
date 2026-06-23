# Weekly Inventory Report Email — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Email a dashboard-styled weekly inventory report (sellable / sample / on-order per SKU) plus a formatted `.xlsx` attachment to a configurable recipient list, on an editable weekday+time schedule, with an admin "Send now" button.

**Architecture:** Reuse `compute_inventory_report(db)` for all data. A new `inventory_report_email` service renders the HTML+text bodies and builds the formatted workbook; an extended `mailer.send_email` carries HTML + attachments. Five new `Shop` columns store recipients + schedule; the existing APScheduler gains an `apply_inventory_report_schedule` job mirroring the SAP-sync pattern. A settings panel + two admin routes on `/reports/inventory` manage it.

**Tech Stack:** FastAPI/Starlette, SQLAlchemy 2.x, Alembic, APScheduler, xlsxwriter (build) + openpyxl (test read-back), stdlib smtplib, Jinja2, pytest (SQLite).

**Spec:** `docs/superpowers/specs/2026-06-23-weekly-inventory-report-email-design.md`

**Branch:** `feature/inventory-report-email` (already checked out).

---

## File structure

- **Create** `app/services/inventory_report_email.py` — `render_inventory_email`, `build_inventory_xlsx`, `send_inventory_report`.
- **Modify** `app/services/mailer.py` — add optional `html` + `attachments` params.
- **Modify** `app/models/shop.py` — 5 columns + `report_recipients_list` property.
- **Create** `alembic/versions/c2d3e4f5a6b7_inventory_report_email_schedule.py` — add the 5 columns.
- **Modify** `app/services/scheduler.py` — `REPORT_JOB_ID`, `_run_inventory_report_job`, `apply_inventory_report_schedule`, register in `start_scheduler`.
- **Modify** `app/routers/reports.py` — extend `inventory_report_view` context; add `POST /reports/inventory/email-settings` and `POST /reports/inventory/send-now` (both admin-only).
- **Modify** `app/routers/exports.py` — add `GET /inventory.xlsx` reusing `build_inventory_xlsx`.
- **Modify** `app/templates/reports/inventory_report.html` — settings panel + flash banner.
- **Tests**: `tests/test_mailer_html_attachments.py`, `tests/test_inventory_report_email.py`, `tests/test_inventory_report_schedule.py`, `tests/test_inventory_report_routes.py`.

Run all tests with: `py -m pytest <path> 2>&1 | tail -20` (Bash tool, per repo convention).

---

## Task 1: Mailer — HTML body + attachments

**Files:**
- Modify: `app/services/mailer.py`
- Test: `tests/test_mailer_html_attachments.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mailer_html_attachments.py
"""mailer.send_email gains optional HTML alternative + attachments, without
breaking the existing text-only callers. The SMTP seam is monkeypatched."""
import smtplib
from email.message import EmailMessage

import app.services.mailer as mailer
from app.config import settings


class _FakeSMTP:
    sent: EmailMessage | None = None

    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def starttls(self): pass
    def login(self, *a): pass
    def send_message(self, msg): _FakeSMTP.sent = msg


def _patch(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.test", raising=False)
    monkeypatch.setattr(settings, "smtp_user", "u@test", raising=False)
    monkeypatch.setattr(settings, "smtp_password", "pw", raising=False)
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    _FakeSMTP.sent = None


def test_text_only_still_works(monkeypatch):
    _patch(monkeypatch)
    mailer.send_email("Subj", "plain body", to=["a@x.com"])
    msg = _FakeSMTP.sent
    assert msg["To"] == "a@x.com"
    assert "plain body" in msg.get_content()


def test_html_and_attachment(monkeypatch):
    _patch(monkeypatch)
    mailer.send_email(
        "Subj", "plain fallback", to=["a@x.com", "b@x.com"],
        html="<p>hi</p>",
        attachments=[("inv.xlsx", b"PK\x03\x04stub", "xlsx")],
    )
    msg = _FakeSMTP.sent
    assert msg["To"] == "a@x.com, b@x.com"
    # HTML alternative present
    html_parts = [p for p in msg.walk()
                  if p.get_content_type() == "text/html"]
    assert html_parts and "<p>hi</p>" in html_parts[0].get_content()
    # Attachment present
    atts = [p for p in msg.walk()
            if p.get_content_disposition() == "attachment"]
    assert len(atts) == 1
    assert atts[0].get_filename() == "inv.xlsx"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -m pytest tests/test_mailer_html_attachments.py -v 2>&1 | tail -20`
Expected: `test_html_and_attachment` FAILS (TypeError: unexpected keyword 'html').

- [ ] **Step 3: Implement the mailer change**

Replace the body of `app/services/mailer.py` with:

```python
"""Outbound email via stdlib smtplib — the single send seam for sync-failure
alerts and the inventory report. No third-party dependency. SMTP config comes
from app.config; tests monkeypatch smtplib.SMTP. Raises on send failure (the
caller decides what to do)."""
from __future__ import annotations

import smtplib
from email.message import EmailMessage

from app.config import settings


def send_email(
    subject: str,
    body: str,
    *,
    to: list[str],
    html: str | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> None:
    """Send an email. `body` is the plain-text content; when `html` is given the
    message becomes multipart/alternative (text + html). `attachments` is a list
    of (filename, payload_bytes, mime_subtype) — e.g. ("inv.xlsx", b"...",
    "xlsx") attaches as application/<subtype>."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.sync_alert_from or settings.smtp_user
    msg["To"] = ", ".join(to)
    msg.set_content(body)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    for filename, payload, subtype in attachments or []:
        msg.add_attachment(
            payload, maintype="application", subtype=subtype, filename=filename
        )
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -m pytest tests/test_mailer_html_attachments.py -v 2>&1 | tail -20`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/mailer.py tests/test_mailer_html_attachments.py
git commit -m "mailer: optional HTML alternative + attachments"
```

---

## Task 2: Shop columns + recipients helper + migration

**Files:**
- Modify: `app/models/shop.py`
- Create: `alembic/versions/c2d3e4f5a6b7_inventory_report_email_schedule.py`
- Test: `tests/test_inventory_report_schedule.py` (model part only here)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_inventory_report_schedule.py
"""Shop inventory-report-email schedule fields + the recipients helper, and
(Task 6) the scheduler job registration."""
from app.db import Base, SessionLocal, engine
from app.models.shop import Shop
import pytest


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def test_recipients_list_parses_and_trims():
    s = Shop(slug="x", name="X",
             inventory_report_recipients=" a@x.com , b@x.com ,, ")
    assert s.report_recipients_list == ["a@x.com", "b@x.com"]


def test_recipients_list_empty():
    s = Shop(slug="x", name="X", inventory_report_recipients="")
    assert s.report_recipients_list == []


def test_schedule_defaults():
    with SessionLocal() as db:
        s = Shop(slug="d", name="D")
        db.add(s); db.commit(); db.refresh(s)
        assert s.inventory_report_enabled is False
        assert s.inventory_report_days == "mon"
        assert s.inventory_report_hour == 8
        assert s.inventory_report_minute == 0
        assert s.inventory_report_recipients == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -m pytest tests/test_inventory_report_schedule.py -v 2>&1 | tail -20`
Expected: FAIL (Shop has no attribute `report_recipients_list` / column missing).

- [ ] **Step 3: Add the columns + helper to `app/models/shop.py`**

Append inside the `Shop` class, after the `inventory_sync_days` column:

```python
    # ---- Weekly inventory-report email (admin-managed on /reports/inventory) --
    # Same scheduling shape as the SAP sync above. Recipients is a comma-
    # separated list; the report emails to all of them. Off + no recipients by
    # default so nothing sends until configured.
    inventory_report_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    inventory_report_hour: Mapped[int] = mapped_column(Integer, default=8)
    inventory_report_minute: Mapped[int] = mapped_column(Integer, default=0)
    inventory_report_days: Mapped[str] = mapped_column(String(64), default="mon")
    inventory_report_recipients: Mapped[str] = mapped_column(String(1024), default="")

    @property
    def report_recipients_list(self) -> list[str]:
        """Recipient emails, parsed + trimmed from the comma-separated column."""
        return [a.strip() for a in (self.inventory_report_recipients or "").split(",")
                if a.strip()]
```

- [ ] **Step 4: Create the Alembic migration**

```python
# alembic/versions/c2d3e4f5a6b7_inventory_report_email_schedule.py
"""inventory report email schedule columns

Adds the admin-managed weekly inventory-report email config to `shops`:
enabled flag + hour/minute (shop timezone) + day_of_week string + recipients.
Defaults: disabled, Monday 08:00, no recipients.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-06-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("shops", schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            "inventory_report_enabled", sa.Boolean(),
            nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column(
            "inventory_report_hour", sa.Integer(),
            nullable=False, server_default="8"))
        batch_op.add_column(sa.Column(
            "inventory_report_minute", sa.Integer(),
            nullable=False, server_default="0"))
        batch_op.add_column(sa.Column(
            "inventory_report_days", sa.String(length=64),
            nullable=False, server_default="mon"))
        batch_op.add_column(sa.Column(
            "inventory_report_recipients", sa.String(length=1024),
            nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("shops", schema=None) as batch_op:
        batch_op.drop_column("inventory_report_recipients")
        batch_op.drop_column("inventory_report_days")
        batch_op.drop_column("inventory_report_minute")
        batch_op.drop_column("inventory_report_hour")
        batch_op.drop_column("inventory_report_enabled")
```

- [ ] **Step 5: Run model tests + migration parity**

Run: `py -m pytest tests/test_inventory_report_schedule.py tests/test_migrations.py -v 2>&1 | tail -20`
Expected: model tests pass; `test_migrations.py` passes (models↔migrations parity holds).

- [ ] **Step 6: Commit**

```bash
git add app/models/shop.py alembic/versions/c2d3e4f5a6b7_inventory_report_email_schedule.py tests/test_inventory_report_schedule.py
git commit -m "shop: inventory-report email schedule columns + migration"
```

---

## Task 3: Email rendering (HTML + text)

**Files:**
- Create: `app/services/inventory_report_email.py`
- Test: `tests/test_inventory_report_email.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_inventory_report_email.py
"""Inventory report email: HTML/text rendering, formatted xlsx, and the send
orchestration (SMTP seam monkeypatched)."""
import io
from datetime import datetime
from decimal import Decimal

import openpyxl

from app.reports.inventory_report import InventoryReportRow, InventoryReportView
import app.services.inventory_report_email as ire


def _view(last_sync=datetime(2026, 6, 23, 7, 30)):
    row = InventoryReportRow(
        canonical_sku="111", sku_code="SBX-OG-PRIMER", name="OG Primer",
        is_bundle=False, sellable_on_hand=640, sample_on_hand=8, total_on_hand=648,
        in_transit=48, unit_cogs=Decimal("3.00"),
        sellable_value=Decimal("1920.00"), sample_value=Decimal("24.00"),
        total_value=Decimal("1944.00"))
    return InventoryReportView(
        rows=[row], total_sellable=640, total_sample=8, total_units=648,
        total_in_transit=48, sku_count=1,
        total_sellable_value=Decimal("1920.00"), total_sample_value=Decimal("24.00"),
        total_inventory_value=Decimal("1944.00"),
        last_synced_at=last_sync, as_of=datetime(2026, 6, 23, 9, 0))


def test_render_html_has_row_totals_and_snapshot():
    subject, html, text = ire.render_inventory_email(_view())
    assert "Inventory" in subject
    # row data
    assert "SBX-OG-PRIMER" in html and "OG Primer" in html
    assert "640" in html and "48" in html
    # totals row
    assert "648" in html  # total units
    # snapshot line in BOTH bodies
    assert "2026-06-23" in html and "2026-06-23" in text
    # inline-styled, not Tailwind classes
    assert "style=" in html and "class=" not in html


def test_render_handles_no_snapshot():
    subject, html, text = ire.render_inventory_email(_view(last_sync=None))
    assert "no snapshot" in html.lower()
    assert "no snapshot" in text.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -m pytest tests/test_inventory_report_email.py -v 2>&1 | tail -20`
Expected: FAIL (module `app.services.inventory_report_email` not found).

- [ ] **Step 3: Create `app/services/inventory_report_email.py` with the renderer**

```python
"""Weekly inventory report email: render the dashboard-styled HTML + text bodies
and a formatted .xlsx attachment from an InventoryReportView, and send it.

Pure formatting + a thin send seam — reuses compute_inventory_report for data
and mailer.send_email for delivery.
"""
from __future__ import annotations

import io
from datetime import datetime

import xlsxwriter
from sqlalchemy.orm import Session

from app.reports.inventory_report import InventoryReportView, compute_inventory_report
from app.services import mailer

# Inline styles approximating the dashboard (email clients strip Tailwind/CSS).
_CARD = ("border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;"
         "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
         "max-width:760px")
_TH = ("padding:8px 12px;text-align:left;font-size:10px;font-weight:600;"
       "text-transform:uppercase;letter-spacing:.05em;color:#64748b;"
       "border-bottom:1px solid #e2e8f0")
_TH_R = _TH + ";text-align:right"
_TD = "padding:8px 12px;font-size:13px;color:#0f172a;border-bottom:1px solid #f1f5f9"
_TD_R = _TD + ";text-align:right;font-variant-numeric:tabular-nums"
_TOT = "padding:8px 12px;font-size:13px;font-weight:700;color:#0f172a;border-top:2px solid #e2e8f0"
_TOT_R = _TOT + ";text-align:right;font-variant-numeric:tabular-nums"


def _snapshot_line(view: InventoryReportView) -> str:
    if view.last_synced_at is None:
        return "No snapshot yet — inventory has not been synced."
    return f"Inventory as of {view.last_synced_at:%Y-%m-%d %H:%M} (shop local)"


def render_inventory_email(view: InventoryReportView) -> tuple[str, str, str]:
    """Return (subject, html_body, text_body). HTML is inline-styled to match the
    dashboard; both bodies state the last-snapshot time up top."""
    date_str = f"{view.as_of:%b %d, %Y}"
    subject = f"Smashbox Weekly Inventory — {date_str}"
    snap = _snapshot_line(view)

    rows_html = []
    for r in view.rows:
        rows_html.append(
            f'<tr><td style="{_TD}">{r.sku_code or "Unmapped"}</td>'
            f'<td style="{_TD}">{(r.name or "")}</td>'
            f'<td style="{_TD_R}">{r.sellable_on_hand}</td>'
            f'<td style="{_TD_R}">{r.sample_on_hand}</td>'
            f'<td style="{_TD_R}">{r.in_transit}</td></tr>'
        )
    totals = (
        f'<tr><td style="{_TOT}" colspan="2">Total · {view.sku_count} SKUs</td>'
        f'<td style="{_TOT_R}">{view.total_sellable}</td>'
        f'<td style="{_TOT_R}">{view.total_sample}</td>'
        f'<td style="{_TOT_R}">{view.total_in_transit}</td></tr>'
    )
    html = (
        f'<div style="{_CARD}">'
        f'<div style="padding:16px 12px;background:#f8fafc">'
        f'<div style="font-size:16px;font-weight:700;color:#0f172a">Smashbox Weekly Inventory</div>'
        f'<div style="font-size:12px;color:#475569;margin-top:2px">{snap}</div></div>'
        f'<table style="width:100%;border-collapse:collapse">'
        f'<thead><tr>'
        f'<th style="{_TH}">SKU</th><th style="{_TH}">Product</th>'
        f'<th style="{_TH_R}">Sellable</th><th style="{_TH_R}">Sample</th>'
        f'<th style="{_TH_R}">On Order</th></tr></thead>'
        f'<tbody>{"".join(rows_html)}{totals}</tbody></table></div>'
    )

    text_lines = [
        "Smashbox Weekly Inventory", snap, "",
        f"{'SKU':<18}{'Product':<28}{'Sellable':>9}{'Sample':>8}{'OnOrder':>8}",
    ]
    for r in view.rows:
        text_lines.append(
            f"{(r.sku_code or 'Unmapped'):<18}{(r.name or '')[:27]:<28}"
            f"{r.sellable_on_hand:>9}{r.sample_on_hand:>8}{r.in_transit:>8}"
        )
    text_lines.append(
        f"{'TOTAL':<18}{f'{view.sku_count} SKUs':<28}"
        f"{view.total_sellable:>9}{view.total_sample:>8}{view.total_in_transit:>8}"
    )
    return subject, html, "\n".join(text_lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -m pytest tests/test_inventory_report_email.py -v 2>&1 | tail -20`
Expected: `test_render_*` pass (xlsx/send tests come in Tasks 4-5 and will error until then — run only the two render tests: append `-k render`).

Run: `py -m pytest tests/test_inventory_report_email.py -k render -v 2>&1 | tail -20`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add app/services/inventory_report_email.py tests/test_inventory_report_email.py
git commit -m "inventory email: dashboard-styled HTML + text rendering"
```

---

## Task 4: Formatted .xlsx builder + download route

**Files:**
- Modify: `app/services/inventory_report_email.py` (add `build_inventory_xlsx`)
- Modify: `app/routers/exports.py` (add `GET /inventory.xlsx`)
- Test: `tests/test_inventory_report_email.py` (add xlsx test)

- [ ] **Step 1: Add the failing test**

Append to `tests/test_inventory_report_email.py`:

```python
def test_build_xlsx_readback():
    data = ire.build_inventory_xlsx(_view())
    assert data[:4] == b"PK\x03\x04"            # xlsx is a zip
    wb = openpyxl.load_workbook(io.BytesIO(data))
    ws = wb.active
    flat = [v for rowvals in ws.iter_rows(values_only=True) for v in rowvals]
    assert "SBX-OG-PRIMER" in flat
    assert any(isinstance(v, str) and "Inventory as of" in v for v in flat)  # caption
    assert any(isinstance(v, str) and v.upper() == "TOTAL" for v in flat)    # totals row
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -m pytest tests/test_inventory_report_email.py::test_build_xlsx_readback -v 2>&1 | tail -20`
Expected: FAIL (`build_inventory_xlsx` not defined).

- [ ] **Step 3: Add `build_inventory_xlsx` to `app/services/inventory_report_email.py`**

```python
def build_inventory_xlsx(view: InventoryReportView) -> bytes:
    """A formatted inventory workbook: frozen bold header, autofilter, column
    widths, integer/money number formats, and a bold totals row. A caption cell
    carries the snapshot age so it travels with the file."""
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("Inventory")

    title = wb.add_format({"bold": True, "font_size": 14})
    caption = wb.add_format({"font_color": "#475569"})
    hdr = wb.add_format({"bold": True, "bg_color": "#f1f5f9", "bottom": 1,
                         "align": "left"})
    hdr_r = wb.add_format({"bold": True, "bg_color": "#f1f5f9", "bottom": 1,
                           "align": "right"})
    money = wb.add_format({"num_format": "$#,##0.00"})
    num = wb.add_format({"num_format": "#,##0"})
    tot = wb.add_format({"bold": True, "top": 2})
    tot_n = wb.add_format({"bold": True, "top": 2, "num_format": "#,##0"})
    tot_m = wb.add_format({"bold": True, "top": 2, "num_format": "$#,##0.00"})

    ws.write("A1", "Smashbox — Weekly Inventory", title)
    # _snapshot_line returns "Inventory as of …" when a snapshot exists; give a
    # parallel "Inventory as of: …" caption when none, so the file always carries
    # the snapshot age (and the test's "Inventory as of" check holds either way).
    ws.write("A2", _snapshot_line(view) if view.last_synced_at
             else "Inventory as of: no snapshot yet", caption)

    headers = ["SKU", "Product", "Sellable", "Sample", "On Order",
               "Total On Hand", "Unit COGS", "Total Value"]
    hrow = 3
    for c, h in enumerate(headers):
        ws.write(hrow, c, h, hdr_r if c >= 2 else hdr)

    r = hrow + 1
    for row in view.rows:
        ws.write(r, 0, row.sku_code or "Unmapped")
        ws.write(r, 1, row.name or "")
        ws.write_number(r, 2, row.sellable_on_hand, num)
        ws.write_number(r, 3, row.sample_on_hand, num)
        ws.write_number(r, 4, row.in_transit, num)
        ws.write_number(r, 5, row.total_on_hand, num)
        ws.write_number(r, 6, float(row.unit_cogs), money)
        ws.write_number(r, 7, float(row.total_value), money)
        r += 1

    ws.write(r, 0, "TOTAL", tot)
    ws.write(r, 1, f"{view.sku_count} SKUs", tot)
    ws.write_number(r, 2, view.total_sellable, tot_n)
    ws.write_number(r, 3, view.total_sample, tot_n)
    ws.write_number(r, 4, view.total_in_transit, tot_n)
    ws.write_number(r, 5, view.total_units, tot_n)
    ws.write_blank(r, 6, None, tot)
    ws.write_number(r, 7, float(view.total_inventory_value), tot_m)

    ws.freeze_panes(hrow + 1, 0)
    ws.autofilter(hrow, 0, r - 1, len(headers) - 1)
    ws.set_column(0, 0, 18)
    ws.set_column(1, 1, 34)
    ws.set_column(2, 7, 13)
    wb.close()
    buf.seek(0)
    return buf.getvalue()
```

Note: simplify the A2 caption — replace the two `ws.write("A2", …)` lines above with this single block:

```python
    ws.write("A2", _snapshot_line(view) if view.last_synced_at
             else "Inventory as of: no snapshot yet", caption)
```

(The `_snapshot_line` already returns "Inventory as of …" when a snapshot exists, satisfying the test's "Inventory as of" check.)

- [ ] **Step 4: Add the download route to `app/routers/exports.py`**

Add near the other inventory export (after `export_inventory_csv`):

```python
@router.get("/inventory.xlsx")
def export_inventory_xlsx(db: Session = Depends(get_db)):
    """Complete inventory as a formatted .xlsx (same builder the weekly email uses)."""
    from app.services.inventory_report_email import build_inventory_xlsx
    view = compute_inventory_report(db)
    data = build_inventory_xlsx(view)
    stamp = view.last_synced_at.strftime("%Y%m%d") if view.last_synced_at else "current"
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="smashbox_inventory_{stamp}.xlsx"'},
    )
```

- [ ] **Step 5: Run tests**

Run: `py -m pytest tests/test_inventory_report_email.py -v 2>&1 | tail -20`
Expected: render + xlsx tests pass (send test still errors until Task 5; run `-k "render or xlsx"`).

- [ ] **Step 6: Commit**

```bash
git add app/services/inventory_report_email.py app/routers/exports.py tests/test_inventory_report_email.py
git commit -m "inventory email: formatted xlsx builder + /inventory.xlsx route"
```

---

## Task 5: Send orchestration

**Files:**
- Modify: `app/services/inventory_report_email.py` (add `send_inventory_report`)
- Test: `tests/test_inventory_report_email.py` (add send tests)

- [ ] **Step 1: Add failing tests**

Append to `tests/test_inventory_report_email.py`:

```python
def test_send_inventory_report_calls_mailer(monkeypatch):
    captured = {}

    def fake_send(subject, body, *, to, html=None, attachments=None):
        captured.update(subject=subject, body=body, to=to, html=html,
                        attachments=attachments)

    monkeypatch.setattr(ire, "compute_inventory_report", lambda db: _view())
    monkeypatch.setattr(ire.mailer, "send_email", fake_send)
    ire.send_inventory_report(db=None, recipients=["a@x.com", "b@x.com"])
    assert captured["to"] == ["a@x.com", "b@x.com"]
    assert "<table" in captured["html"]
    assert captured["attachments"][0][0].endswith(".xlsx")
    assert captured["attachments"][0][2] == "xlsx"


def test_send_inventory_report_rejects_empty_recipients():
    import pytest
    with pytest.raises(ValueError):
        ire.send_inventory_report(db=None, recipients=[])
```

- [ ] **Step 2: Run to verify failure**

Run: `py -m pytest tests/test_inventory_report_email.py -k send -v 2>&1 | tail -20`
Expected: FAIL (`send_inventory_report` not defined).

- [ ] **Step 3: Add `send_inventory_report`**

Append to `app/services/inventory_report_email.py`:

```python
def send_inventory_report(db: Session, *, recipients: list[str]) -> None:
    """Compute the current inventory report, render it, attach the formatted
    workbook, and email it to `recipients`. Raises ValueError on empty
    recipients; propagates any send error to the caller."""
    if not recipients:
        raise ValueError("no recipients configured for the inventory report")
    view = compute_inventory_report(db)
    subject, html, text = render_inventory_email(view)
    xlsx = build_inventory_xlsx(view)
    stamp = view.last_synced_at.strftime("%Y%m%d") if view.last_synced_at else "current"
    mailer.send_email(
        subject, text, to=recipients, html=html,
        attachments=[(f"smashbox_inventory_{stamp}.xlsx", xlsx, "xlsx")],
    )
```

- [ ] **Step 4: Run all email tests**

Run: `py -m pytest tests/test_inventory_report_email.py -v 2>&1 | tail -20`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/services/inventory_report_email.py tests/test_inventory_report_email.py
git commit -m "inventory email: send orchestration (compute -> render -> attach -> mail)"
```

---

## Task 6: Scheduler job

**Files:**
- Modify: `app/services/scheduler.py`
- Test: `tests/test_inventory_report_schedule.py` (add scheduler tests)

- [ ] **Step 1: Add failing tests**

Append to `tests/test_inventory_report_schedule.py`:

```python
import app.services.scheduler as sched


class _FakeScheduler:
    def __init__(self): self.jobs = {}
    def add_job(self, func, trigger=None, id=None, **k): self.jobs[id] = trigger
    def get_job(self, jid): return self.jobs.get(jid)
    def remove_job(self, jid): self.jobs.pop(jid, None)


def test_apply_report_schedule_registers_when_enabled_with_recipients(monkeypatch):
    fake = _FakeScheduler()
    monkeypatch.setattr(sched, "_scheduler", fake)
    shop = Shop(slug="s", name="S", timezone="America/Los_Angeles",
                inventory_report_enabled=True, inventory_report_days="mon,thu",
                inventory_report_hour=8, inventory_report_minute=0,
                inventory_report_recipients="a@x.com")
    sched.apply_inventory_report_schedule(shop)
    assert sched.REPORT_JOB_ID in fake.jobs


def test_apply_report_schedule_skips_without_recipients(monkeypatch):
    fake = _FakeScheduler()
    monkeypatch.setattr(sched, "_scheduler", fake)
    shop = Shop(slug="s", name="S", timezone="America/Los_Angeles",
                inventory_report_enabled=True, inventory_report_days="mon",
                inventory_report_recipients="")
    sched.apply_inventory_report_schedule(shop)
    assert sched.REPORT_JOB_ID not in fake.jobs


def test_apply_report_schedule_removes_when_disabled(monkeypatch):
    fake = _FakeScheduler()
    fake.jobs[sched.REPORT_JOB_ID] = object()
    monkeypatch.setattr(sched, "_scheduler", fake)
    shop = Shop(slug="s", name="S", timezone="America/Los_Angeles",
                inventory_report_enabled=False,
                inventory_report_recipients="a@x.com")
    sched.apply_inventory_report_schedule(shop)
    assert sched.REPORT_JOB_ID not in fake.jobs
```

- [ ] **Step 2: Run to verify failure**

Run: `py -m pytest tests/test_inventory_report_schedule.py -k report_schedule -v 2>&1 | tail -20`
Expected: FAIL (`REPORT_JOB_ID` / `apply_inventory_report_schedule` not defined).

- [ ] **Step 3: Implement in `app/services/scheduler.py`**

Add the job id constant near the others:

```python
REPORT_JOB_ID = "inventory_report_email"
```

Add the job runner (after `_run_tiktok_sync_job`):

```python
def _run_inventory_report_job() -> None:
    """Scheduler entry point: email the weekly inventory report. Own DB session;
    never propagates exceptions. On failure, log and (if the sync-alert channel
    is configured) send a failure alert so the operator knows it didn't go out."""
    from app.services.inventory_report_email import send_inventory_report

    with SessionLocal() as db:
        shop = _primary_shop(db)
        if shop is None:
            return
        try:
            send_inventory_report(db, recipients=shop.report_recipients_list)
            logger.info("inventory report emailed to %d recipient(s)",
                        len(shop.report_recipients_list))
        except Exception:  # noqa: BLE001
            logger.exception("scheduled inventory report email failed")
            _alert_report_failure()


def _alert_report_failure() -> None:
    """Best-effort failure alert via the existing sync-alert channel."""
    try:
        from app.services import mailer
        if settings.sync_alerts_enabled:
            mailer.send_email(
                "⚠ Smashbox inventory report failed",
                "The scheduled weekly inventory report email failed to send. "
                "Check SMTP config and the app logs.",
                to=settings.sync_alert_to_list,
            )
    except Exception:  # noqa: BLE001
        logger.exception("inventory report failure-alert also failed")
```

Add the apply function (after `apply_inventory_schedule`):

```python
def apply_inventory_report_schedule(shop: Shop) -> None:
    """Register / reschedule / remove the weekly inventory-report email job to
    match ``shop``. Registered only when enabled AND recipients exist. Safe to
    call when the scheduler isn't running (no-op)."""
    if _scheduler is None:
        return

    if not (shop.inventory_report_enabled and shop.report_recipients_list):
        if _scheduler.get_job(REPORT_JOB_ID):
            _scheduler.remove_job(REPORT_JOB_ID)
            logger.info("inventory report email disabled — job removed")
        return

    trigger = CronTrigger(
        day_of_week=shop.inventory_report_days,
        hour=shop.inventory_report_hour,
        minute=shop.inventory_report_minute,
        timezone=shop.timezone,
    )
    _scheduler.add_job(
        _run_inventory_report_job,
        trigger=trigger,
        id=REPORT_JOB_ID,
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=3600,
        max_instances=1,
    )
    logger.info(
        "inventory report email scheduled: %s %02d:%02d %s (%d recipients)",
        shop.inventory_report_days, shop.inventory_report_hour,
        shop.inventory_report_minute, shop.timezone,
        len(shop.report_recipients_list),
    )
```

Register it in `start_scheduler()` — inside the `if shop is not None:` block, after `apply_tiktok_schedule(shop)`:

```python
            apply_inventory_report_schedule(shop)
```

- [ ] **Step 4: Run tests**

Run: `py -m pytest tests/test_inventory_report_schedule.py -v 2>&1 | tail -20`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/services/scheduler.py tests/test_inventory_report_schedule.py
git commit -m "scheduler: weekly inventory-report email job + failure alert"
```

---

## Task 7: Admin routes (settings + send-now)

**Files:**
- Modify: `app/routers/reports.py`
- Test: `tests/test_inventory_report_routes.py`

**Reference:** `app/routers/uploads.py::update_inventory_sync_settings` (validation pattern), `_VALID_DAYS = ["mon","tue","wed","thu","fri","sat","sun"]`, `from app.auth import require_admin`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_inventory_report_routes.py
"""Inventory-report email settings + send-now routes. Admin-guarded; settings
persist on Shop and reschedule; send-now invokes the send seam."""
import pytest
from starlette.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.models.shop import Shop
import app.routers.reports as reports_mod


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        db.add(Shop(slug="smashbox", name="Smashbox")); db.commit()
    yield


def _client(monkeypatch):
    # Bypass admin auth cleanly via FastAPI dependency_overrides (monkeypatching
    # auth.require_admin would NOT affect the dependency already bound into the
    # route at import time).
    from app.auth import require_admin
    from app.main import app
    app.dependency_overrides[require_admin] = lambda: None
    return TestClient(app)


def test_settings_persist_and_reschedule(monkeypatch):
    calls = {}
    monkeypatch.setattr(reports_mod, "apply_inventory_report_schedule",
                        lambda shop: calls.setdefault("rescheduled", True))
    client = _client(monkeypatch)
    resp = client.post("/reports/inventory/email-settings", data={
        "recipients": "a@x.com, b@x.com", "enabled": "1",
        "days": ["mon", "thu"], "report_time": "08:30"},
        follow_redirects=False)
    assert resp.status_code == 303
    with SessionLocal() as db:
        shop = db.query(Shop).first()
        assert shop.inventory_report_enabled is True
        assert shop.inventory_report_days == "mon,thu"
        assert shop.inventory_report_hour == 8 and shop.inventory_report_minute == 30
        assert shop.report_recipients_list == ["a@x.com", "b@x.com"]
    assert calls.get("rescheduled")


def test_send_now_invokes_send(monkeypatch):
    sent = {}
    monkeypatch.setattr(reports_mod, "send_inventory_report",
                        lambda db, recipients: sent.setdefault("to", recipients))
    client = _client(monkeypatch)
    with SessionLocal() as db:
        shop = db.query(Shop).first()
        shop.inventory_report_recipients = "ops@x.com"; db.commit()
    resp = client.post("/reports/inventory/send-now", follow_redirects=False)
    assert resp.status_code == 303
    assert sent["to"] == ["ops@x.com"]
```

- [ ] **Step 2: Run to verify failure**

Run: `py -m pytest tests/test_inventory_report_routes.py -v 2>&1 | tail -20`
Expected: FAIL (routes 404 / names not importable).

- [ ] **Step 3: Implement the routes in `app/routers/reports.py`**

Add imports near the top (with the other imports):

```python
from app.auth import require_admin
from app.models.shop import Shop
from app.services.inventory_report_email import send_inventory_report
from app.services.scheduler import apply_inventory_report_schedule

_REPORT_VALID_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
```

Replace the existing `inventory_report_view` with a version that adds `shop` + flash context:

```python
@router.get("/reports/inventory")
def inventory_report_view(request: Request, sent: str | None = None,
                          err: str | None = None, db: Session = Depends(get_db)):
    """Complete inventory: every SKU with sellable (SB) + sample (SBS) on-hand,
    plus the admin email-settings panel."""
    view = compute_inventory_report(db)
    shop = db.query(Shop).order_by(Shop.id).first()
    return templates.TemplateResponse(
        request, "reports/inventory_report.html",
        {"view": view, "shop": shop, "valid_days": _REPORT_VALID_DAYS,
         "flash_sent": sent, "flash_err": err,
         "smtp_configured": bool(settings.smtp_host)},
    )
```

Add the two POST routes (admin-guarded):

```python
@router.post("/reports/inventory/email-settings",
             dependencies=[Depends(require_admin)])
def update_inventory_report_settings(
    recipients: str = Form(""),
    report_time: str = Form("08:00"),
    enabled: str | None = Form(None),
    days: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    """Persist the weekly inventory-report email config and live-reschedule."""
    shop = db.query(Shop).order_by(Shop.id).first()
    if shop is None:
        raise HTTPException(status_code=404, detail="no shop configured")
    try:
        hh, mm = report_time.split(":")
        hour, minute = int(hh), int(mm)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail=f"bad time {report_time!r}")

    chosen = [d for d in _REPORT_VALID_DAYS if d in set(days)]
    clean = [a.strip() for a in recipients.split(",") if a.strip()]
    shop.inventory_report_recipients = ",".join(clean)
    shop.inventory_report_enabled = bool(enabled is not None and chosen and clean)
    shop.inventory_report_hour = hour
    shop.inventory_report_minute = minute
    if chosen:
        shop.inventory_report_days = ",".join(chosen)
    db.commit()
    apply_inventory_report_schedule(shop)
    return RedirectResponse("/reports/inventory", status_code=303)


@router.post("/reports/inventory/send-now",
             dependencies=[Depends(require_admin)])
def send_inventory_report_now(db: Session = Depends(get_db)):
    """Email the inventory report immediately to the saved recipients."""
    shop = db.query(Shop).order_by(Shop.id).first()
    if shop is None or not shop.report_recipients_list:
        return RedirectResponse("/reports/inventory?err=no-recipients", status_code=303)
    try:
        send_inventory_report(db, recipients=shop.report_recipients_list)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(f"/reports/inventory?err=send-failed", status_code=303)
    return RedirectResponse("/reports/inventory?sent=ok", status_code=303)
```

Confirm these are already imported at the top of `reports.py` (add any missing): `Form`, `HTTPException`, `RedirectResponse`, `Depends`, `Request`, `settings`. (`settings` import: `from app.config import settings`.)

- [ ] **Step 4: Run tests**

Run: `py -m pytest tests/test_inventory_report_routes.py -v 2>&1 | tail -25`
Expected: both pass. If auth bypass needs adjusting, stub `require_admin` via FastAPI `app.dependency_overrides[require_admin] = lambda: None` instead of monkeypatching.

- [ ] **Step 5: Commit**

```bash
git add app/routers/reports.py tests/test_inventory_report_routes.py
git commit -m "reports: admin inventory-report email settings + send-now routes"
```

---

## Task 8: Settings panel UI

**Files:**
- Modify: `app/templates/reports/inventory_report.html`

**Reference:** `app/templates/uploads.html` lines ~84-111 (schedule markup: enabled checkbox, `type="time"` input, weekday checkboxes).

- [ ] **Step 1: Add the panel + flash banner**

Near the top of the page content (inside the main block, before the inventory table), add:

```html
{% if flash_sent %}<div class="mb-3 rounded-md bg-emerald-50 px-3 py-2 text-sm text-emerald-700 print:hidden">Inventory report sent.</div>{% endif %}
{% if flash_err == 'no-recipients' %}<div class="mb-3 rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-700 print:hidden">Add at least one recipient before sending.</div>{% endif %}
{% if flash_err == 'send-failed' %}<div class="mb-3 rounded-md bg-rose-50 px-3 py-2 text-sm text-rose-700 print:hidden">Send failed — check SMTP config and logs.</div>{% endif %}

{% if shop %}
{% set report_days = shop.inventory_report_days.split(",") %}
<details class="mb-5 rounded-xl border border-slate-200 bg-white shadow-sm print:hidden">
  <summary class="cursor-pointer list-none px-4 py-3 text-sm font-semibold text-slate-700">
    📧 Weekly email report
    <span class="ml-2 text-xs font-normal text-slate-400">
      {% if shop.inventory_report_enabled %}on · {{ shop.inventory_report_days }} {{ '%02d:%02d'|format(shop.inventory_report_hour, shop.inventory_report_minute) }}{% else %}off{% endif %}
    </span>
  </summary>
  <div class="border-t border-slate-100 px-4 py-4">
    {% if not smtp_configured %}
    <p class="mb-3 rounded bg-amber-50 px-2 py-1 text-xs text-amber-700">SMTP is not configured — emails will not send until it is.</p>
    {% endif %}
    <form method="post" action="/reports/inventory/email-settings" class="space-y-3 text-sm">
      <div>
        <label class="block text-xs font-medium text-slate-500">Recipients (comma-separated)</label>
        <input type="text" name="recipients" value="{{ shop.inventory_report_recipients }}"
               placeholder="ops@smashbox.com, finance@smashbox.com"
               class="mt-1 w-full rounded-md border border-slate-300 px-2 py-1.5">
      </div>
      <div class="flex flex-wrap items-center gap-3">
        <label class="inline-flex items-center gap-1.5">
          <input type="checkbox" name="enabled" value="1" {% if shop.inventory_report_enabled %}checked{% endif %}>
          <span class="text-slate-600">Enabled</span>
        </label>
        <label class="inline-flex items-center gap-1.5">
          <span class="text-xs text-slate-500">Time</span>
          <input type="time" name="report_time"
                 value="{{ '%02d:%02d'|format(shop.inventory_report_hour, shop.inventory_report_minute) }}"
                 class="rounded-md border border-slate-300 px-2 py-1">
        </label>
        <div class="flex items-center gap-1">
          {% for d in valid_days %}
          <label class="cursor-pointer rounded-md border border-slate-300 px-2 py-1 text-xs has-[:checked]:border-slate-900 has-[:checked]:bg-slate-900 has-[:checked]:text-white">
            <input type="checkbox" name="days" value="{{ d }}" class="peer sr-only" {% if d in report_days %}checked{% endif %}>
            {{ d|capitalize }}
          </label>
          {% endfor %}
        </div>
      </div>
      <div class="flex items-center gap-2">
        <button type="submit" class="rounded-md bg-slate-900 px-3 py-1.5 font-medium text-white">Save schedule</button>
      </div>
    </form>
    <form method="post" action="/reports/inventory/send-now" class="mt-3 border-t border-slate-100 pt-3">
      <button type="submit" class="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">Send now</button>
      <span class="ml-2 text-xs text-slate-400">Emails the report immediately to the saved recipients.</span>
    </form>
  </div>
</details>
{% endif %}
```

- [ ] **Step 2: Verify the page renders (route smoke test)**

Add to `tests/test_inventory_report_routes.py`:

```python
def test_inventory_page_renders_panel(monkeypatch):
    client = _client(monkeypatch)
    resp = client.get("/reports/inventory")
    assert resp.status_code == 200
    assert "Weekly email report" in resp.text
    assert 'action="/reports/inventory/email-settings"' in resp.text
```

Run: `py -m pytest tests/test_inventory_report_routes.py::test_inventory_page_renders_panel -v 2>&1 | tail -20`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add app/templates/reports/inventory_report.html tests/test_inventory_report_routes.py
git commit -m "inventory page: weekly email-report settings panel + flash"
```

---

## Task 9: Full suite + manual verification

- [ ] **Step 1: Run the full test suite**

Run: `py -m pytest 2>&1 | tail -15`
Expected: all pass (902 baseline + the new tests), no new failures.

- [ ] **Step 2: Manual eyeball (local)**

Per CLAUDE.md, restart uvicorn after Python edits:
```
# kill stale python, then:
py -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```
Open `http://127.0.0.1:8000/reports/inventory`, expand **📧 Weekly email report**, confirm the recipients field, weekday chips, time input, **Save schedule**, and **Send now** render. Download **/inventory.xlsx** and confirm the formatted workbook (frozen header, autofilter, totals row, caption). *(Email send requires SMTP — not exercised locally; the send seam is unit-tested.)*

- [ ] **Step 3: Confirm migration applies cleanly (optional, against a scratch DB)**

Run: `py -m alembic upgrade head 2>&1 | tail -5` (only against a disposable DB; do NOT run against the dev DB if it must stay put). The release command applies it in prod automatically.

- [ ] **Step 4: Final commit / branch ready for merge**

The branch `feature/inventory-report-email` is ready. Deploy uses the standard flow (branch → main ff merge → `fly deploy`); the `release_command` runs `alembic upgrade head` to add the columns before the new code goes live.

---

## Self-review notes (coverage vs spec)

- Multiple recipients → Task 2 (`report_recipients_list`) + Task 7 (parse/persist) + Task 5 (send to all). ✓
- Dashboard-matched inline-styled HTML + snapshot line → Task 3. ✓
- Formatted xlsx attachment + snapshot caption → Task 4. ✓
- Sellable / sample / on-order + SKU + name columns → Tasks 3 & 4 (from existing `InventoryReportView`). ✓
- Recipients UI + weekday/time scheduler on the inventory page → Tasks 7 & 8. ✓
- Admin-only send-now + settings → Task 7 (`require_admin`). ✓
- Weekly multi-day schedule in shop tz → Task 6 (`CronTrigger`). ✓
- Failure alert via sync-alert channel → Task 6 (`_alert_report_failure`). ✓
- Migration (Postgres) + parity → Task 2. ✓
