"""Smashbox fiscal calendar on the P&L: each fiscal month runs the 29th → 28th
and is LABELED by its closing month (convention A). Fiscal May 2026 =
Apr 29 – May 28; Fiscal Year 2026 = Dec 29 2025 – Dec 28 2026; Fiscal YTD
through M = fiscal Jan..M.

Covers the window math (incl. the non-leap-February edge), the inclusive-start /
exclusive-end boundary, the multi-month breakdown counts, window_for, and the
page render + selector gating.
"""
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import (
    ImportBatch,
    ImportBatchStatus,
    ImportFileKind,
    Order,
    OrderLine,
    OrderType,
)
from app.reports.pnl import (
    PeriodKind,
    _fiscal_window,
    compute_pnl_view,
    window_for,
)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _order(db, bid, placed_at, *, tt_id, gross=Decimal("100.00")):
    o = Order(import_batch_id=bid, tiktok_order_id=tt_id, placed_at=placed_at,
              order_type=OrderType.PAID, status="Shipped", brand="smashbox",
              gross_sales=gross)
    db.add(o)
    db.flush()
    db.add(OrderLine(order_id=o.id, sku="SBX-001", quantity=1,
                     gross_sales=gross, unit_cogs_snapshot=Decimal("0")))


def _batch(db):
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                    original_filename="t", stored_path="t")
    db.add(b); db.flush()
    return b


# ---- Window math (pure) ----------------------------------------------------

def test_fiscal_window_convention_a():
    assert _fiscal_window(2026, 5) == (date(2026, 4, 29), date(2026, 5, 28))
    assert _fiscal_window(2026, 1) == (date(2025, 12, 29), date(2026, 1, 28))  # year boundary
    assert _fiscal_window(2026, 12) == (date(2026, 11, 29), date(2026, 12, 28))


def test_fiscal_window_february_edge():
    # Non-leap 2026: "29th of Feb" doesn't exist, so fiscal March opens Mar 1.
    assert _fiscal_window(2026, 3) == (date(2026, 3, 1), date(2026, 3, 28))
    # Leap 2024: Feb 29 exists, so fiscal March opens Feb 29.
    assert _fiscal_window(2024, 3) == (date(2024, 2, 29), date(2024, 3, 28))


# ---- Fiscal month boundary (inclusive start, exclusive end) ----------------

def test_fiscal_month_includes_29th_through_28th_only():
    with SessionLocal() as db:
        b = _batch(db)
        _order(db, b.id, datetime(2026, 4, 28, 12), tt_id="before")   # out (closes prior fiscal)
        _order(db, b.id, datetime(2026, 4, 29, 0, 0), tt_id="open")    # in  (fiscal May opens)
        _order(db, b.id, datetime(2026, 5, 28, 23, 59), tt_id="close") # in  (fiscal May closes)
        _order(db, b.id, datetime(2026, 5, 29, 0, 0), tt_id="after")   # out (next fiscal opens)
        db.commit()
        view = compute_pnl_view(db, PeriodKind.FISCAL_MONTH, 2026, 5)
    assert view.title_suffix == "Fiscal May 2026 (Apr 29, 2026 – May 28, 2026)"
    assert view.monthly_breakdown is None
    # No discounts/refunds → Net Customer Sales == included gross == 2 × $100.
    assert view.total.net_customer_sales == Decimal("200.00")
    assert view.total.orders_count == 2


def test_window_for_fiscal_month():
    with SessionLocal() as db:
        view = compute_pnl_view(db, PeriodKind.FISCAL_MONTH, 2026, 5)
    start, end = window_for(view)
    assert start == datetime(2026, 4, 29)
    assert end == datetime(2026, 5, 29)   # exclusive (day after the 28th)


# ---- Fiscal year + YTD breakdown -------------------------------------------

def test_fiscal_year_spans_dec29_to_dec28_with_12_columns():
    with SessionLocal() as db:
        b = _batch(db)
        _order(db, b.id, datetime(2025, 12, 29, 12), tt_id="fy-open")  # first day of FY2026
        _order(db, b.id, datetime(2026, 12, 28, 12), tt_id="fy-close") # last day of FY2026
        _order(db, b.id, datetime(2025, 12, 28, 12), tt_id="prev-fy")  # belongs to FY2025
        db.commit()
        view = compute_pnl_view(db, PeriodKind.FISCAL_YEAR, 2026)
    assert view.title_suffix == "Fiscal Year 2026 (Dec 29, 2025 – Dec 28, 2026)"
    assert len(view.monthly_breakdown) == 12            # one column per fiscal month
    assert view.total.orders_count == 2                 # the Dec 28 2025 order is excluded
    s, e = window_for(view)
    assert (s, e) == (datetime(2025, 12, 29), datetime(2026, 12, 29))


def test_fiscal_ytd_through_may_has_five_columns():
    with SessionLocal() as db:
        b = _batch(db)
        _order(db, b.id, datetime(2026, 5, 10, 12), tt_id="in-ytd")
        _order(db, b.id, datetime(2026, 6, 10, 12), tt_id="after-ytd")  # fiscal June, excluded
        db.commit()
        view = compute_pnl_view(db, PeriodKind.FISCAL_YTD, 2026, 5)
    assert "Fiscal YTD through May 2026" in view.title_suffix
    assert "Dec 29, 2025 – May 28, 2026" in view.title_suffix
    assert len(view.monthly_breakdown) == 5             # Jan..May
    assert view.total.orders_count == 1
    s, e = window_for(view)
    assert (s, e) == (datetime(2025, 12, 29), datetime(2026, 5, 29))


# ---- Page render + selector gating -----------------------------------------

def test_pnl_page_renders_fiscal_month(client):
    r = client.get("/reports/pnl?period=fiscal_month&year=2026&month=5")
    assert r.status_code == 200
    assert "Fiscal May 2026" in r.text
    assert "Apr 29, 2026" in r.text and "May 28, 2026" in r.text


def test_pnl_page_renders_fiscal_year_multicolumn(client):
    r = client.get("/reports/pnl?period=fiscal_year&year=2026")
    assert r.status_code == 200
    assert "Fiscal Year 2026" in r.text
    assert "Dec 29, 2025 – Dec 28, 2026" in r.text


def test_fiscal_period_banner_shows_only_for_fiscal_scopes(client):
    fiscal = client.get("/reports/pnl?period=fiscal_month&year=2026&month=5").text
    assert "Fiscal Period" in fiscal                       # accent callout present
    assert "Apr 29, 2026" in fiscal and "May 28, 2026" in fiscal
    calendar_view = client.get("/reports/pnl?period=month&year=2026&month=5").text
    assert "Fiscal Period" not in calendar_view            # not on calendar months


def test_fiscal_options_on_pnl_but_not_dashboard(client):
    pnl = client.get("/reports/pnl").text
    assert 'value="fiscal_month"' in pnl
    assert 'value="fiscal_year"' in pnl
    dash = client.get("/").text
    assert 'value="fiscal_month"' not in dash    # gated off on the Dashboard selector
