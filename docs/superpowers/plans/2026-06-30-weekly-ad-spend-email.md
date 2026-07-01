# Weekly Ad-Spend Email Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a weekly emailable Ad-Spend report — a budget block (gross spend since the current budget period start + remaining for the year, from the existing AdBudget engine) and a per-day gross-spend table for the previous complete week (Mon–Sun) — wired into the existing report-email machinery (settings card, scheduler, "Send now").

**Architecture:** A pure composed view (`compute_ad_spend_email_view` → `AdSpendEmailView`) reads GMV-Max daily cost directly (same source the budget engine uses, so the week total ties to the budget's spend basis) and pulls budget figures from `compute_budget_view(current_budget(db))`. Render/CSV/send functions consume only that view. Config lives on 5 new `Shop` columns; an APScheduler cron and two admin routes drive it. No new budget model; the email reads the live editable budget amount, so mid-period top-ups are reflected automatically.

**Tech Stack:** FastAPI, SQLAlchemy 2.x, Jinja2, APScheduler, Alembic, pytest. Local/tests use SQLite; prod uses Postgres.

**Spec:** `docs/superpowers/specs/2026-06-30-weekly-ad-spend-email-design.md`

---

## Reference patterns (read before starting)

- Email service pattern: `app/services/sales_report_email.py`
- Shared email CSS + `resolve_rolling_period` + `register_report_job`: `app/services/report_email_common.py`
- Budget engine: `app/reports/ad_budget.py` (`current_budget`, `compute_budget_view` → `AdBudgetView`)
- Scheduler wiring: `app/services/scheduler.py` (`_run_sales_report_job`, `apply_sales_report_schedule`, `start_scheduler`)
- Shop columns + recipients property: `app/models/shop.py`
- Routes: `app/routers/reports.py` (`update_sales_report_settings`, `send_sales_report_now`, `ad_spend_view`)
- Settings-card partial: `app/templates/partials/report_email_settings.html`
- Email-service test pattern: `tests/test_sales_report_email.py`
- Route test pattern: `tests/test_sales_email_routes.py`

**Test invocation (per project memory):** run pytest via the Bash tool as
`py -m pytest <args> 2>&1 | tail -40`. Before any foreground pytest, ensure no
background suite is running (shared `smashbox_tests.sqlite` collides).

---

## Task 1: Composed view `compute_ad_spend_email_view`

**Files:**
- Create: `app/reports/ad_spend_email.py`
- Test: `tests/test_ad_spend_email_view.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ad_spend_email_view.py
"""Composed view for the weekly ad-spend email: a zero-filled 7-day Mon–Sun grid
of GMV-Max gross spend + budget figures pulled from the AdBudget engine."""
import itertools
from datetime import date
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.gmv_max_daily_metric import GmvMaxDailyMetric
from app.models.ad_budget import AdBudget
from app.reports.ad_spend_email import compute_ad_spend_email_view


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


_BID = itertools.count(1)


def _metric(db, d, cost):
    b = ImportBatch(kind=ImportFileKind.TIKTOK_GMV_MAX, status=ImportBatchStatus.COMPLETED,
                    original_filename="g", stored_path="g")
    db.add(b); db.flush()
    db.add(GmvMaxDailyMetric(import_batch_id=b.id, metric_date=d,
                             cost=Decimal(str(cost)), sku_orders=1,
                             gross_revenue=Decimal("0")))
    db.flush()


# Week under test: Mon 2026-06-22 .. Sun 2026-06-28
WK_START = date(2026, 6, 22)
WK_END = date(2026, 6, 28)


def test_week_grid_is_seven_days_zero_filled_and_totaled():
    with SessionLocal() as db:
        _metric(db, date(2026, 6, 22), 100)   # Mon
        _metric(db, date(2026, 6, 24), 50)    # Wed
        db.commit()
        view = compute_ad_spend_email_view(db, week_start=WK_START, week_end=WK_END,
                                           today=date(2026, 6, 29))
    assert [d.day for d in view.days] == [date(2026, 6, 22 + i) for i in range(7)]
    assert view.days[0].gross_spend == Decimal("100")   # Mon
    assert view.days[1].gross_spend == Decimal("0")     # Tue zero-filled
    assert view.days[2].gross_spend == Decimal("50")    # Wed
    assert view.week_total == Decimal("150")


def test_budget_block_pulled_from_current_budget():
    with SessionLocal() as db:
        db.add(AdBudget(label="FY26-27", start_date=date(2026, 7, 1),
                        end_date=date(2027, 6, 30), amount=Decimal("35000")))
        # spend inside the budget window, before "today"
        _metric(db, date(2026, 7, 3), 500)
        db.commit()
        view = compute_ad_spend_email_view(
            db, week_start=date(2026, 7, 6), week_end=date(2026, 7, 12),
            today=date(2026, 7, 13))
    assert view.has_budget is True
    assert view.budget_amount == Decimal("35000")
    assert view.spend_since_start == Decimal("500")
    assert view.remaining == Decimal("34500")
    assert view.is_over_budget is False


def test_no_budget_covering_today():
    with SessionLocal() as db:
        _metric(db, date(2026, 6, 22), 100); db.commit()
        view = compute_ad_spend_email_view(db, week_start=WK_START, week_end=WK_END,
                                           today=date(2026, 6, 29))
    assert view.has_budget is False
    assert view.budget_amount == Decimal("0")
    assert len(view.days) == 7          # week table still present
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest tests/test_ad_spend_email_view.py -q 2>&1 | tail -20`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.reports.ad_spend_email'`.

- [ ] **Step 3: Write the minimal implementation**

```python
# app/reports/ad_spend_email.py
"""Composed view for the weekly ad-spend email.

Pairs a budget tracker (gross spend since the current budget period start +
remaining for the year, from the AdBudget engine) with a zero-filled 7-day
Mon–Sun grid of GMV-Max gross spend for a given week. The week grid reads
`GmvMaxDailyMetric.cost` directly — the SAME source the budget engine sums — so
the week total ties to the budget's spend basis. Pure computation: reads the
ORM, returns dataclasses, writes nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.gmv_max_daily_metric import GmvMaxDailyMetric
from app.reports.ad_budget import compute_budget_view, current_budget


@dataclass
class AdSpendEmailDay:
    day: date
    gross_spend: Decimal


@dataclass
class AdSpendEmailView:
    week_start: date
    week_end: date
    days: list[AdSpendEmailDay] = field(default_factory=list)   # exactly 7, Mon..Sun
    week_total: Decimal = Decimal("0")

    has_budget: bool = False
    budget_label: str | None = None
    budget_start: date | None = None
    budget_end: date | None = None
    budget_amount: Decimal = Decimal("0")
    spend_since_start: Decimal = Decimal("0")   # AdBudgetView.total_ad_spend
    remaining: Decimal = Decimal("0")           # AdBudgetView.available
    pct_used: Decimal = Decimal("0")
    is_over_budget: bool = False
    days_remaining: int = 0
    projected_total: Decimal = Decimal("0")


def _week_spend(db: Session, week_start: date, week_end: date) -> dict[date, Decimal]:
    """GMV-Max cost per day in [week_start, week_end] inclusive (days with rows)."""
    rows = db.execute(
        select(GmvMaxDailyMetric.metric_date, GmvMaxDailyMetric.cost)
        .where(GmvMaxDailyMetric.metric_date >= week_start)
        .where(GmvMaxDailyMetric.metric_date <= week_end)
    ).all()
    return {d: Decimal(str(c or 0)) for d, c in rows}


def compute_ad_spend_email_view(
    db: Session, *, week_start: date, week_end: date, today: date | None = None
) -> AdSpendEmailView:
    spend = _week_spend(db, week_start, week_end)
    days: list[AdSpendEmailDay] = []
    total = Decimal("0")
    d = week_start
    while d <= week_end:
        amt = spend.get(d, Decimal("0"))
        days.append(AdSpendEmailDay(day=d, gross_spend=amt))
        total += amt
        d += timedelta(days=1)

    view = AdSpendEmailView(week_start=week_start, week_end=week_end,
                            days=days, week_total=total)

    budget = current_budget(db, today=today)
    if budget is not None:
        bv = compute_budget_view(db, budget, today=today)
        view.has_budget = True
        view.budget_label = budget.label
        view.budget_start = budget.start_date
        view.budget_end = budget.end_date
        view.budget_amount = bv.budget_amount
        view.spend_since_start = bv.total_ad_spend
        view.remaining = bv.available
        view.pct_used = bv.pct_used
        view.is_over_budget = bv.is_over_budget
        view.days_remaining = bv.days_remaining
        view.projected_total = bv.projected_total
    return view
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -m pytest tests/test_ad_spend_email_view.py -q 2>&1 | tail -20`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add app/reports/ad_spend_email.py tests/test_ad_spend_email_view.py
git commit -F .git/COMMIT_MSG_DRAFT.txt
```
(Write the message to `.git/COMMIT_MSG_DRAFT.txt` first — per project memory, HEREDOC commits truncate on this setup. Message:)
```
ad-spend email: composed view (budget block + zero-filled weekly grid)

compute_ad_spend_email_view reads GMV-Max daily cost directly (same source
the budget engine sums) and pulls budget figures from compute_budget_view(
current_budget(db)). Pure view; render/csv/send land next.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

---

## Task 2: Email builder (render / CSV / send)

**Files:**
- Create: `app/services/ad_spend_report_email.py`
- Test: `tests/test_ad_spend_report_email.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ad_spend_report_email.py
"""Ad-spend email: render (budget block + 7-day table), CSV builder, send seam."""
import itertools
from datetime import date
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.gmv_max_daily_metric import GmvMaxDailyMetric
from app.models.ad_budget import AdBudget
from app.services import ad_spend_report_email as ase


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _metric(db, d, cost):
    b = ImportBatch(kind=ImportFileKind.TIKTOK_GMV_MAX, status=ImportBatchStatus.COMPLETED,
                    original_filename="g", stored_path="g")
    db.add(b); db.flush()
    db.add(GmvMaxDailyMetric(import_batch_id=b.id, metric_date=d,
                             cost=Decimal(str(cost)), sku_orders=1,
                             gross_revenue=Decimal("0")))
    db.flush()


def _seed_budget_and_week(db):
    db.add(AdBudget(label="FY26-27", start_date=date(2026, 7, 1),
                    end_date=date(2027, 6, 30), amount=Decimal("35000")))
    _metric(db, date(2026, 7, 6), 100)    # Mon of the test week
    _metric(db, date(2026, 7, 8), 50)     # Wed
    db.commit()


def test_render_has_budget_block_and_seven_day_table():
    from app.reports.ad_spend_email import compute_ad_spend_email_view
    with SessionLocal() as db:
        _seed_budget_and_week(db)
        view = compute_ad_spend_email_view(
            db, week_start=date(2026, 7, 6), week_end=date(2026, 7, 12),
            today=date(2026, 7, 13))
    subject, html, text = ase.render_ad_spend_email(view)
    assert "Ad Spend" in subject
    # budget figures present
    assert "35,000.00" in html
    assert "remaining" in html.lower()
    # week total = 150; both day values present
    assert "150.00" in html
    assert "100.00" in html and "50.00" in html
    # 7 day rows rendered (count the currency cells in the week table is 7 + total)
    assert html.count("Gross spend") == 1   # single table header
    assert "150.00" in text


def test_render_no_budget_note():
    from app.reports.ad_spend_email import compute_ad_spend_email_view
    with SessionLocal() as db:
        _metric(db, date(2026, 7, 6), 100); db.commit()
        view = compute_ad_spend_email_view(
            db, week_start=date(2026, 7, 6), week_end=date(2026, 7, 12),
            today=date(2026, 7, 13))
    _, html, text = ase.render_ad_spend_email(view)
    assert "No active ad budget" in html
    assert "No active ad budget" in text


def test_csv_has_budget_summary_and_daily_rows():
    from app.reports.ad_spend_email import compute_ad_spend_email_view
    with SessionLocal() as db:
        _seed_budget_and_week(db)
        view = compute_ad_spend_email_view(
            db, week_start=date(2026, 7, 6), week_end=date(2026, 7, 12),
            today=date(2026, 7, 13))
    csv = ase.build_ad_spend_csv(view).decode()
    assert "Remaining,34850.00" in csv          # 35000 - 150
    assert "Day,Gross spend" in csv
    assert "2026-07-06,100.00" in csv
    assert "Week total,150.00" in csv
    # 7 daily rows between the header and the total
    body = csv.split("Day,Gross spend")[1].strip().splitlines()
    assert len(body) == 8                        # 7 days + total line


def test_send_uses_mailer_for_previous_week(monkeypatch):
    calls = {}
    def fake_send(subject, body, *, to, html=None, attachments=None):
        calls.update(subject=subject, to=to, html=html, attachments=attachments)
    monkeypatch.setattr(ase.mailer, "send_email", fake_send)
    with SessionLocal() as db:
        _seed_budget_and_week(db)
        # today = Mon 2026-07-13 → previous week = Mon 07-06 .. Sun 07-12
        ase.send_ad_spend_report(db, recipients=["a@x.com"], today=date(2026, 7, 13))
    assert calls["to"] == ["a@x.com"]
    assert calls["html"] and len(calls["attachments"]) == 1
    assert calls["attachments"][0][0].endswith(".csv")
    assert calls["attachments"][0][2] == "csv"
    # the week's spend (150) made it into the attachment
    assert "150.00" in calls["attachments"][0][1].decode()


def test_send_requires_recipients():
    with SessionLocal() as db, pytest.raises(ValueError):
        ase.send_ad_spend_report(db, recipients=[], today=date(2026, 7, 13))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest tests/test_ad_spend_report_email.py -q 2>&1 | tail -20`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.ad_spend_report_email'`.

- [ ] **Step 3: Write the minimal implementation**

```python
# app/services/ad_spend_report_email.py
"""Email the weekly Ad-Spend report: an inline-styled HTML budget block + a
per-day gross-spend table for the previous complete week (Mon–Sun), plus a CSV
attachment built from the same view. Thin send seam over mailer.send_email."""
from __future__ import annotations

import csv
import io
from html import escape

from sqlalchemy.orm import Session

from app.reports.ad_spend_email import compute_ad_spend_email_view
from app.services import mailer
from app.services.report_email_common import (
    CARD, HEADER, H_TITLE, H_SUB, TH, TH_R, TD, TD_R, TOT, TOT_R,
    resolve_rolling_period,
)
from app.services.reporting_tz import today_local


def _budget_html(view) -> str:
    if not view.has_budget:
        return (f'<div style="{CARD}"><div style="{HEADER}">'
                f'<div style="{H_TITLE}">Ad Budget</div>'
                f'<div style="{H_SUB}">No active ad budget covers this week.</div>'
                f'</div></div>')
    color = "#b91c1c" if view.is_over_budget else "#0f172a"
    return (
        f'<div style="{CARD}"><div style="{HEADER}">'
        f'<div style="{H_TITLE}">Ad Budget — {escape(view.budget_label or "")}</div>'
        f'<div style="{H_SUB}">{view.budget_start:%b %d, %Y} – {view.budget_end:%b %d, %Y}</div>'
        f'<div style="font-size:22px;font-weight:700;color:{color};margin-top:6px">'
        f'${view.remaining:,.2f} remaining</div>'
        f'<div style="{H_SUB}">Allocated ${view.budget_amount:,.2f} · '
        f'Spent since {view.budget_start:%b %d} ${view.spend_since_start:,.2f} · '
        f'{view.pct_used:.0f}% used · Projected ${view.projected_total:,.2f} · '
        f'{view.days_remaining} days left</div>'
        f'</div></div>'
    )


def render_ad_spend_email(view):
    """(subject, html, text). Budget block + a 7-row Mon–Sun spend table, both
    from the same view so the HTML and the CSV attachment always match."""
    subject = f"Smashbox Ad Spend — week of {view.week_start:%b %d}"

    rows = []
    for d in view.days:
        rows.append(
            f'<tr><td style="{TD}">{d.day:%a %b %d}</td>'
            f'<td style="{TD_R}">${d.gross_spend:,.2f}</td></tr>'
        )
    total = (
        f'<tr><td style="{TOT}">Week total</td>'
        f'<td style="{TOT_R}">${view.week_total:,.2f}</td></tr>'
    )
    week_card = (
        f'<div style="{CARD}"><div style="{HEADER}">'
        f'<div style="{H_TITLE}">This week</div>'
        f'<div style="{H_SUB}">{view.week_start:%b %d} – {view.week_end:%b %d, %Y} (Mon–Sun)</div></div>'
        f'<table style="width:100%;border-collapse:collapse"><thead><tr>'
        f'<th style="{TH}">Day</th><th style="{TH_R}">Gross spend</th></tr></thead>'
        f'<tbody>{"".join(rows)}{total}</tbody></table></div>'
    )
    html = _budget_html(view) + week_card

    text_lines = [f"Smashbox Ad Spend — week of {view.week_start:%b %d}", ""]
    if view.has_budget:
        text_lines += [
            f"Budget {view.budget_label}: ${view.remaining:,.2f} remaining "
            f"(allocated ${view.budget_amount:,.2f}, spent ${view.spend_since_start:,.2f}, "
            f"{view.pct_used:.0f}% used, {view.days_remaining} days left)", ""]
    else:
        text_lines += ["No active ad budget covers this week.", ""]
    text_lines.append(f"This week {view.week_start:%b %d} – {view.week_end:%b %d, %Y}")
    for d in view.days:
        text_lines.append(f"{d.day:%a %b %d}   ${d.gross_spend:>12,.2f}")
    text_lines.append(f"{'Week total':<10}   ${view.week_total:>12,.2f}")
    return subject, html, "\n".join(text_lines)


def build_ad_spend_csv(view) -> bytes:
    """Budget summary block + a Day,Gross spend section for the 7 days + a total."""
    buf = io.StringIO()
    w = csv.writer(buf)
    if view.has_budget:
        w.writerow(["Budget", view.budget_label])
        w.writerow(["Period", f"{view.budget_start.isoformat()} to {view.budget_end.isoformat()}"])
        w.writerow(["Allocated", f"{view.budget_amount:.2f}"])
        w.writerow(["Spent since start", f"{view.spend_since_start:.2f}"])
        w.writerow(["Remaining", f"{view.remaining:.2f}"])
        w.writerow(["Pct used", f"{view.pct_used:.2f}"])
        w.writerow(["Projected", f"{view.projected_total:.2f}"])
        w.writerow(["Days remaining", view.days_remaining])
    else:
        w.writerow(["Budget", "none active"])
    w.writerow([])
    w.writerow(["Day", "Gross spend"])
    for d in view.days:
        w.writerow([d.day.isoformat(), f"{d.gross_spend:.2f}"])
    w.writerow(["Week total", f"{view.week_total:.2f}"])
    return buf.getvalue().encode()


def _previous_week(today):
    """(monday, sunday) of the week before `today`, via the shared resolver."""
    w = resolve_rolling_period("prev_week", today=today)
    return w.start, w.end


def send_ad_spend_report(db: Session, *, recipients, today=None) -> None:
    """Build the previous-complete-week view, render, attach the CSV, send."""
    if not recipients:
        raise ValueError("no recipients configured for the ad-spend report")
    today = today or today_local()
    week_start, week_end = _previous_week(today)
    view = compute_ad_spend_email_view(db, week_start=week_start, week_end=week_end,
                                       today=today)
    subject, html, text = render_ad_spend_email(view)
    csv_bytes = build_ad_spend_csv(view)
    fname = f"smashbox_ad_spend_{week_start:%Y%m%d}_{week_end:%Y%m%d}.csv"
    mailer.send_email(subject, text, to=recipients, html=html,
                      attachments=[(fname, csv_bytes, "csv")])
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -m pytest tests/test_ad_spend_report_email.py -q 2>&1 | tail -20`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```
ad-spend email: render / CSV / send seam

render_ad_spend_email + build_ad_spend_csv (budget block + 7-row Mon–Sun
table, HTML and CSV from the same view). send_ad_spend_report resolves the
previous complete week via resolve_rolling_period("prev_week") and sends
through mailer with the CSV attachment. Raises ValueError on empty recipients.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
```bash
git add app/services/ad_spend_report_email.py tests/test_ad_spend_report_email.py
git commit -F .git/COMMIT_MSG_DRAFT.txt
```

---

## Task 3: Shop columns + property + Alembic migration

**Files:**
- Modify: `app/models/shop.py` (add 5 columns after the sample-report block, ~line 77; add property after `sample_report_recipients_list`, ~line 91)
- Create: `alembic/versions/<newrev>_ad_spend_report_email.py`
- Test: `tests/test_ad_spend_shop_columns.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ad_spend_shop_columns.py
"""Shop gains ad-spend-report email settings columns + a recipients-list property."""
import pytest

from app.db import Base, SessionLocal, engine
from app.models.shop import Shop


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def test_defaults_and_recipients_list():
    with SessionLocal() as db:
        db.add(Shop(slug="smashbox", name="Smashbox")); db.commit()
        shop = db.query(Shop).first()
        assert shop.ad_spend_report_enabled is False
        assert shop.ad_spend_report_hour == 8
        assert shop.ad_spend_report_minute == 0
        assert shop.ad_spend_report_days == "mon"
        assert shop.ad_spend_report_recipients == ""
        assert shop.ad_spend_report_recipients_list == []
        shop.ad_spend_report_recipients = "a@x.com, b@x.com "
        assert shop.ad_spend_report_recipients_list == ["a@x.com", "b@x.com"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `py -m pytest tests/test_ad_spend_shop_columns.py -q 2>&1 | tail -20`
Expected: FAIL — `AttributeError: 'Shop' object has no attribute 'ad_spend_report_enabled'`.

- [ ] **Step 3a: Add the columns to `app/models/shop.py`**

Insert immediately after the sample-report block (after the `sample_report_period` line, ~line 77):

```python
    # ---- Ad-spend-report email (managed on /reports/ad-spend) -----------------
    # Weekly budget-tracker + previous-week daily spend. Fixed window (previous
    # Mon–Sun + current budget period), so no period column. Off + no recipients
    # by default so nothing sends until configured.
    ad_spend_report_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    ad_spend_report_hour: Mapped[int] = mapped_column(Integer, default=8)
    ad_spend_report_minute: Mapped[int] = mapped_column(Integer, default=0)
    ad_spend_report_days: Mapped[str] = mapped_column(String(64), default="mon")
    ad_spend_report_recipients: Mapped[str] = mapped_column(String(1024), default="")
```

Insert after `sample_report_recipients_list` (end of file, ~line 91):

```python
    @property
    def ad_spend_report_recipients_list(self) -> list[str]:
        return [a.strip() for a in (self.ad_spend_report_recipients or "").split(",") if a.strip()]
```

- [ ] **Step 3b: Run the test to verify it passes (SQLite `create_all` path)**

Run: `py -m pytest tests/test_ad_spend_shop_columns.py -q 2>&1 | tail -20`
Expected: PASS (1 passed). (Tests build the schema via `create_all`, so they pass before the migration exists — the migration is for Postgres/prod.)

- [ ] **Step 3c: Create the Alembic migration**

First find the current head:

Run: `py -m alembic heads 2>&1 | tail -5`
Expected: exactly one head id (e.g. `d8e9f0a1b2c3 (head)`). Note that id — use it as `down_revision` below. If MORE than one head prints, STOP and report — the migration graph has diverged and needs a merge revision first (out of scope for this task).

Create `alembic/versions/a1a2a3a4a5a6_ad_spend_report_email.py` (pick any unused 12-hex `revision` id; set `down_revision` to the head printed above):

```python
"""ad-spend report email settings on shops

Revision ID: a1a2a3a4a5a6
Revises: <PASTE_CURRENT_HEAD_ID_HERE>
Create Date: 2026-06-30
"""
from alembic import op
import sqlalchemy as sa

revision = "a1a2a3a4a5a6"
down_revision = "<PASTE_CURRENT_HEAD_ID_HERE>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("shops", sa.Column("ad_spend_report_enabled", sa.Boolean(),
                                     nullable=False, server_default=sa.false()))
    op.add_column("shops", sa.Column("ad_spend_report_hour", sa.Integer(),
                                     nullable=False, server_default="8"))
    op.add_column("shops", sa.Column("ad_spend_report_minute", sa.Integer(),
                                     nullable=False, server_default="0"))
    op.add_column("shops", sa.Column("ad_spend_report_days", sa.String(length=64),
                                     nullable=False, server_default="mon"))
    op.add_column("shops", sa.Column("ad_spend_report_recipients", sa.String(length=1024),
                                     nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("shops", "ad_spend_report_recipients")
    op.drop_column("shops", "ad_spend_report_days")
    op.drop_column("shops", "ad_spend_report_minute")
    op.drop_column("shops", "ad_spend_report_hour")
    op.drop_column("shops", "ad_spend_report_enabled")
```

- [ ] **Step 3d: Verify the migration applies and models↔migrations parity holds**

Run: `py -m pytest tests/test_migrations.py -q 2>&1 | tail -30`
Expected: PASS. (This test builds from migrations and diffs against the models — it catches a mismatch between the new columns and the migration.)

If `test_migrations.py` reports a diff, reconcile the column types/defaults in the migration with `app/models/shop.py` until it passes.

- [ ] **Step 4: Run both tests together**

Run: `py -m pytest tests/test_ad_spend_shop_columns.py tests/test_migrations.py -q 2>&1 | tail -20`
Expected: PASS.

- [ ] **Step 5: Commit**

```
ad-spend email: Shop settings columns + migration

5 columns (enabled/hour/minute/days/recipients) + recipients_list property,
mirroring the sales/sample report-email settings. No period column — the
ad-spend email window is fixed (previous Mon–Sun + current budget period).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
```bash
git add app/models/shop.py alembic/versions/a1a2a3a4a5a6_ad_spend_report_email.py tests/test_ad_spend_shop_columns.py
git commit -F .git/COMMIT_MSG_DRAFT.txt
```

---

## Task 4: Scheduler wiring

**Files:**
- Modify: `app/services/scheduler.py` (add job-id constant ~line 37; add `_run_ad_spend_report_job` after `_run_sample_report_job` ~line 213; add `apply_ad_spend_report_schedule` after `apply_sample_report_schedule` ~line 367; call it in `start_scheduler` ~line 433)
- Test: `tests/test_ad_spend_report_schedule.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ad_spend_report_schedule.py
"""apply_ad_spend_report_schedule registers the cron only when enabled AND
recipients are set, and removes it otherwise."""
import pytest

import app.services.scheduler as sch
from app.models.shop import Shop


class _FakeScheduler:
    def __init__(self):
        self.jobs = {}
    def get_job(self, job_id):
        return self.jobs.get(job_id)
    def add_job(self, fn, *, trigger, id, **kw):
        self.jobs[id] = trigger
    def remove_job(self, job_id):
        self.jobs.pop(job_id, None)


@pytest.fixture
def fake_scheduler(monkeypatch):
    fake = _FakeScheduler()
    monkeypatch.setattr(sch, "_scheduler", fake)
    return fake


def _shop(**kw):
    s = Shop(slug="smashbox", name="Smashbox", timezone="America/Los_Angeles")
    s.ad_spend_report_enabled = kw.get("enabled", True)
    s.ad_spend_report_recipients = kw.get("recipients", "a@x.com")
    s.ad_spend_report_days = "mon"
    s.ad_spend_report_hour = 8
    s.ad_spend_report_minute = 0
    return s


def test_registers_when_enabled_with_recipients(fake_scheduler):
    sch.apply_ad_spend_report_schedule(_shop())
    assert sch.AD_SPEND_REPORT_JOB_ID in fake_scheduler.jobs


def test_not_registered_when_disabled(fake_scheduler):
    sch.apply_ad_spend_report_schedule(_shop(enabled=False))
    assert sch.AD_SPEND_REPORT_JOB_ID not in fake_scheduler.jobs


def test_removed_when_recipients_cleared(fake_scheduler):
    sch.apply_ad_spend_report_schedule(_shop())
    sch.apply_ad_spend_report_schedule(_shop(recipients=""))
    assert sch.AD_SPEND_REPORT_JOB_ID not in fake_scheduler.jobs
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `py -m pytest tests/test_ad_spend_report_schedule.py -q 2>&1 | tail -20`
Expected: FAIL — `AttributeError: module 'app.services.scheduler' has no attribute 'AD_SPEND_REPORT_JOB_ID'`.

- [ ] **Step 3a: Add the job-id constant** (after `SAMPLE_REPORT_JOB_ID`, ~line 37):

```python
AD_SPEND_REPORT_JOB_ID = "ad_spend_report_email"
```

- [ ] **Step 3b: Add the run-job function** (after `_run_sample_report_job`, before `_alert_report_failure`, ~line 213):

```python
def _run_ad_spend_report_job() -> None:
    """Scheduler entry point: email the weekly Ad-Spend report for the previous
    complete week (+ the current budget block). Own DB session; never propagates
    exceptions. On failure, log and fire a failure alert."""
    from app.services.ad_spend_report_email import send_ad_spend_report

    with SessionLocal() as db:
        shop = _primary_shop(db)
        if shop is None:
            logger.warning("ad-spend report job fired but no primary shop found; skipping")
            return
        if not shop.ad_spend_report_enabled:
            logger.warning("ad-spend report job fired but disabled; skipping")
            return
        recipients = shop.ad_spend_report_recipients_list
        if not recipients:
            logger.warning("ad-spend report job fired with no recipients; skipping")
            return
        try:
            send_ad_spend_report(db, recipients=recipients)
            logger.info("ad-spend report emailed to %d recipient(s)", len(recipients))
        except Exception:  # noqa: BLE001
            logger.exception("scheduled ad-spend report email failed")
            _alert_report_failure("ad-spend")
```

- [ ] **Step 3c: Add the apply-schedule function** (after `apply_sample_report_schedule`, ~line 367):

```python
def apply_ad_spend_report_schedule(shop: Shop) -> None:
    """Register / reschedule / remove the Ad-Spend-report email job to match
    ``shop``. Thin wrapper over the generic register helper."""
    from app.services.report_email_common import register_report_job
    register_report_job(
        _scheduler, AD_SPEND_REPORT_JOB_ID,
        enabled=shop.ad_spend_report_enabled,
        recipients=shop.ad_spend_report_recipients_list,
        days=shop.ad_spend_report_days,
        hour=shop.ad_spend_report_hour,
        minute=shop.ad_spend_report_minute,
        timezone=shop.timezone,
        run_fn=_run_ad_spend_report_job,
    )
```

- [ ] **Step 3d: Call it in `start_scheduler`** (after `apply_sample_report_schedule(shop)`, ~line 433):

```python
            apply_ad_spend_report_schedule(shop)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `py -m pytest tests/test_ad_spend_report_schedule.py -q 2>&1 | tail -20`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```
ad-spend email: scheduler job + apply/reschedule wiring

AD_SPEND_REPORT_JOB_ID, _run_ad_spend_report_job (guards disabled/no-recipients,
fires failure alert on error), apply_ad_spend_report_schedule via the shared
register_report_job helper, registered in start_scheduler.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
```bash
git add app/services/scheduler.py tests/test_ad_spend_report_schedule.py
git commit -F .git/COMMIT_MSG_DRAFT.txt
```

---

## Task 5: Routes + settings card on the page

**Files:**
- Modify: `app/routers/reports.py` (import `send_ad_spend_report` + `apply_ad_spend_report_schedule`; extend `ad_spend_view` context; add two POST routes after `ad_spend_view`, ~line 1599)
- Modify: `app/templates/reports/ad_spend.html` (include the settings-card partial after the page header, ~line 30)
- Test: `tests/test_ad_spend_email_routes.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ad_spend_email_routes.py
"""Ad-spend email settings + send-now routes. Admin-guarded; settings persist on
Shop + reschedule; send-now invokes the send seam; the card renders."""
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


@pytest.fixture
def client():
    from app.auth import require_admin
    from app.main import app
    app.dependency_overrides[require_admin] = lambda: None
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(require_admin, None)


def test_settings_persist_days_and_enabled(monkeypatch, client):
    calls = {}
    monkeypatch.setattr(reports_mod, "apply_ad_spend_report_schedule",
                        lambda shop: calls.setdefault("rescheduled", True))
    resp = client.post("/reports/ad-spend/email-settings", data={
        "recipients": "a@x.com, b@x.com", "enabled": "1",
        "days": ["mon", "thu"], "report_time": "08:30"},
        follow_redirects=False)
    assert resp.status_code == 303
    with SessionLocal() as db:
        shop = db.query(Shop).first()
        assert shop.ad_spend_report_enabled is True
        assert shop.ad_spend_report_days == "mon,thu"
        assert shop.ad_spend_report_hour == 8 and shop.ad_spend_report_minute == 30
        assert shop.ad_spend_report_recipients_list == ["a@x.com", "b@x.com"]
    assert calls.get("rescheduled")


def test_send_now_invokes_send(monkeypatch, client):
    from app.services import ad_spend_report_email as ase
    sent = {}
    monkeypatch.setattr(ase.mailer, "send_email",
                        lambda *a, **k: sent.setdefault("called", True))
    with SessionLocal() as db:
        shop = db.query(Shop).first()
        shop.ad_spend_report_recipients = "ops@x.com"; db.commit()
    resp = client.post("/reports/ad-spend/send-now", data={}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/reports/ad-spend?sent=ok"
    assert sent.get("called")


def test_send_now_no_recipients(client):
    resp = client.post("/reports/ad-spend/send-now", data={}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/reports/ad-spend?err=no-recipients"


def test_page_renders_card(client):
    resp = client.get("/reports/ad-spend")
    assert resp.status_code == 200
    assert "Email Ad Spend report" in resp.text
    assert 'action="/reports/ad-spend/email-settings"' in resp.text
    assert 'action="/reports/ad-spend/send-now"' in resp.text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `py -m pytest tests/test_ad_spend_email_routes.py -q 2>&1 | tail -20`
Expected: FAIL — the POST routes 404 / the page lacks the card / import errors.

- [ ] **Step 3a: Add imports** to `app/routers/reports.py`.

Extend the existing scheduler import (~line 78) to add `apply_ad_spend_report_schedule`:

```python
from app.services.scheduler import (
    apply_inventory_report_schedule,
    apply_sales_report_schedule,
    apply_sample_report_schedule,
    apply_ad_spend_report_schedule,
)
```

Add the send seam near the other report-email imports (~line 76):

```python
from app.services.ad_spend_report_email import send_ad_spend_report
```

- [ ] **Step 3b: Extend the `ad_spend_view` context.**

In `app/routers/reports.py`, in `ad_spend_view` (~line 1581), before the `return templates.TemplateResponse(...)`, add:

```python
    shop = db.query(Shop).order_by(Shop.id).first()
```

Then add these keys to the context dict passed to `TemplateResponse` (alongside `"monthly": monthly,` etc.):

```python
            "shop": shop,
            "valid_days": _REPORT_VALID_DAYS,
            "smtp_configured": bool(settings.smtp_host),
            "flash_sent": request.query_params.get("sent"),
            "flash_err": request.query_params.get("err"),
```

- [ ] **Step 3c: Add the two POST routes** after `ad_spend_view` (immediately before `@router.get("/reports/ad-spend/reimbursements")`, ~line 1599):

```python
@router.post("/reports/ad-spend/email-settings",
             dependencies=[Depends(require_admin)])
def update_ad_spend_report_settings(
    recipients: str = Form(""),
    report_time: str = Form("08:00"),
    enabled: str | None = Form(None),
    days: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    """Persist the Ad-Spend-report email config and live-reschedule."""
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
    shop.ad_spend_report_recipients = ",".join(clean)
    shop.ad_spend_report_hour = hour
    shop.ad_spend_report_minute = minute
    shop.ad_spend_report_enabled = bool(enabled is not None and chosen and clean)
    if chosen:
        shop.ad_spend_report_days = ",".join(chosen)
    db.commit()
    apply_ad_spend_report_schedule(shop)
    return RedirectResponse("/reports/ad-spend?sent=settings", status_code=303)


@router.post("/reports/ad-spend/send-now",
             dependencies=[Depends(require_admin)])
def send_ad_spend_report_now(db: Session = Depends(get_db)):
    """Email the Ad-Spend report immediately (previous complete week +
    current budget block)."""
    shop = db.query(Shop).order_by(Shop.id).first()
    if shop is None or not shop.ad_spend_report_recipients_list:
        return RedirectResponse("/reports/ad-spend?err=no-recipients", status_code=303)
    try:
        send_ad_spend_report(db, recipients=shop.ad_spend_report_recipients_list)
    except Exception:  # noqa: BLE001
        return RedirectResponse("/reports/ad-spend?err=send-failed", status_code=303)
    return RedirectResponse("/reports/ad-spend?sent=ok", status_code=303)
```

- [ ] **Step 3d: Add the settings card to the template.**

In `app/templates/reports/ad_spend.html`, add an import near the top (after line 3):

```jinja
{% from "partials/report_email_settings.html" import card as email_card %}
```

Immediately after the page-header `{% endcall %}` (~line 30), insert:

```jinja
{{ email_card(
    action_prefix="/reports/ad-spend",
    recipients=(shop.ad_spend_report_recipients if shop else ""),
    enabled=(shop.ad_spend_report_enabled if shop else False),
    days_csv=(shop.ad_spend_report_days if shop else "mon"),
    hour=(shop.ad_spend_report_hour if shop else 8),
    minute=(shop.ad_spend_report_minute if shop else 0),
    period="prev_week",
    periods=[("prev_week", "Previous week (Mon–Sun)")],
    valid_days=valid_days,
    scope_fields="",
    smtp_configured=smtp_configured,
    flash_sent=flash_sent,
    flash_err=flash_err,
    report_name="Ad Spend report"
) }}
```

Note: the window is fixed, so the card's period `<select>` is passed a single
locked option and the settings route ignores the posted `period` field. The
`send-now` form needs no scope hidden inputs, so `scope_fields=""`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `py -m pytest tests/test_ad_spend_email_routes.py -q 2>&1 | tail -20`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```
ad-spend email: settings + send-now routes and page card

Admin-guarded /reports/ad-spend/email-settings (persist 5 fields, reschedule)
and /send-now (previous-week send). ad_spend_view gains the card context; the
page includes the shared report_email_settings card with a fixed
previous-week window (single locked period option, no send-now scope fields).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
```bash
git add app/routers/reports.py app/templates/reports/ad_spend.html tests/test_ad_spend_email_routes.py
git commit -F .git/COMMIT_MSG_DRAFT.txt
```

---

## Task 6: Full-suite green + manual eyeball

- [ ] **Step 1: Run the whole suite**

Run: `py -m pytest -q 2>&1 | tail -30`
Expected: all pass (0 failures). Fix any regressions before proceeding.

- [ ] **Step 2: Boot the dev server and eyeball the page**

Per project memory (local dev server backgrounding pattern), start uvicorn with an
explicit `&` + PID + readiness curl inside one Bash call, having first run
`npm run css` if the stylesheet isn't built. Then:

```bash
curl -s localhost:8000/reports/ad-spend | grep -c "Email Ad Spend report"
```
Expected: `1` (the card renders). Because CLAUDE.md warns `--reload` misses Python
changes on Windows, restart uvicorn before this check.

Then visually confirm (curl-200 is NOT visual verification, per memory): the
budget block + "Send now" render, and — with recipients saved and SMTP configured
locally OR a monkeypatched send — a "Send now" produces a real email whose budget
figures and 7-day table match `/admin/ad-budget` and `/reports/ad-spend?scope=daily`.

- [ ] **Step 3: Final commit if any fixups were needed**

```bash
git add -A && git commit -F .git/COMMIT_MSG_DRAFT.txt
```
(Only if Step 1/2 required changes.)

---

## Deploy (after the user confirms the eyeball pass)

Per project memory (local merge, no PR): push the feature branch, `git checkout main`
+ ff-only pull + `--no-ff` merge + push main + `fly deploy`. `fly.toml`'s release
command runs `alembic upgrade head`, applying the Task-3 migration to prod Postgres
automatically. After deploy: verify via `fly releases` + a prod curl of
`/reports/ad-spend` (not the "not listening" warning, per memory), then configure
recipients + Monday 08:00 PT on the page and fire one "Send now" to confirm a real
inbox delivery. Then tear down the worktree.

---

## Self-Review (completed by plan author)

**Spec coverage:** view (§1)→Task 1; render/CSV/send (§2)→Task 2; Shop columns +
migration (§3)→Task 3; scheduler (§3)→Task 4; routes + page card (§3)→Task 5; edge
cases (no-budget, zero-week, over-budget, empty-recipients)→covered by Task 1/2/4/5
tests; testing (§Testing)→Tasks 1,2,3,4,5. All spec sections map to a task.

**Type consistency:** `AdSpendEmailView` / `AdSpendEmailDay` field names are used
identically across Task 1 (definition), Task 2 (render/CSV/send consumers), and the
tests. `AD_SPEND_REPORT_JOB_ID`, `apply_ad_spend_report_schedule`,
`send_ad_spend_report`, `compute_ad_spend_email_view` names match across tasks.
`ad_spend_report_*` Shop columns + `ad_spend_report_recipients_list` match across
Tasks 3/4/5.

**Placeholder scan:** the only deferred value is the migration `down_revision`,
which is intentionally resolved at implementation time via `alembic heads` (Task 3
Step 3c) with a verifying `test_migrations.py` gate — a deterministic command, not a
vague TODO.
