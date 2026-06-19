"""compute_sales_report: bucketing per granularity, the GMV revenue formula,
trend-delta excludes the in-progress bucket, peak, empty window, and parity
with MonthlyPnL.gmv. Seeds PAID orders; no network."""
import itertools
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.order import Order, OrderLine, OrderType
from app.reports.sales_report import compute_sales_report

_OID = itertools.count(1)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _seed(db, d: date, *, gross, units, order_type=OrderType.PAID, **money):
    """One order placed at noon on day `d` (noon avoids tz-shift day crossing),
    with `units` total via a single OrderLine, plus optional discount/ship money
    fields (shipping_revenue, seller_funded_outlandish, seller_funded_smashbox,
    platform_discount_total, payment_platform_discount)."""
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                    original_filename="t", stored_path="t")
    db.add(b); db.flush()
    o = Order(import_batch_id=b.id, tiktok_order_id=f"O{next(_OID)}",
              placed_at=datetime(d.year, d.month, d.day, 12, 0),
              order_type=order_type, status="Completed", brand="smashbox",
              gross_sales=Decimal(str(gross)),
              **{k: Decimal(str(v)) for k, v in money.items()})
    db.add(o); db.flush()
    db.add(OrderLine(order_id=o.id, sku="X", quantity=units))
    db.flush()
    return o


def test_daily_buckets_revenue_units_orders():
    with SessionLocal() as db:
        _seed(db, date(2026, 5, 10), gross=100, units=2)
        _seed(db, date(2026, 5, 10), gross=40, units=1)   # same day -> same bucket
        db.commit()
        view = compute_sales_report(db, "daily", as_of=date(2026, 5, 15))
    bucket = next(b for b in view.buckets if b.key == "2026-05-10")
    assert bucket.revenue == Decimal("140.00")   # 100 + 40, no discounts
    assert bucket.units == 3                       # 2 + 1
    assert bucket.orders == 2
    assert view.total_orders == 2


def test_revenue_applies_gmv_formula():
    with SessionLocal() as db:
        # gross 100 + ship 10 - out 5 - smash 3 - platform 4 - pay 2 = 96
        _seed(db, date(2026, 5, 10), gross=100, units=1, shipping_revenue=10,
              seller_funded_outlandish=5, seller_funded_smashbox=3,
              platform_discount_total=4, payment_platform_discount=2)
        db.commit()
        view = compute_sales_report(db, "daily", as_of=date(2026, 5, 12))
    assert next(b for b in view.buckets if b.key == "2026-05-10").revenue == Decimal("96.00")


def test_monthly_rolls_up_within_a_month():
    with SessionLocal() as db:
        _seed(db, date(2026, 5, 3), gross=100, units=1)
        _seed(db, date(2026, 5, 20), gross=50, units=1)
        db.commit()
        view = compute_sales_report(db, "monthly", as_of=date(2026, 5, 25))
    may = next(b for b in view.buckets if b.key == "2026-05")
    assert may.revenue == Decimal("150.00")
    assert may.orders == 2


def test_samples_excluded():
    with SessionLocal() as db:
        _seed(db, date(2026, 5, 10), gross=100, units=1)
        _seed(db, date(2026, 5, 10), gross=0, units=1, order_type=OrderType.SAMPLE)
        db.commit()
        view = compute_sales_report(db, "daily", as_of=date(2026, 5, 12))
    assert view.total_orders == 1
    assert view.total_revenue == Decimal("100.00")


def test_trend_delta_excludes_in_progress_bucket():
    # daily: today = 2026-05-15 (in-progress). Prior two complete days carry the delta.
    with SessionLocal() as db:
        _seed(db, date(2026, 5, 13), gross=100, units=1)   # prior-complete
        _seed(db, date(2026, 5, 14), gross=150, units=1)   # last-complete
        _seed(db, date(2026, 5, 15), gross=999, units=1)   # in-progress (excluded)
        db.commit()
        view = compute_sales_report(db, "daily", as_of=date(2026, 5, 15))
    # delta compares 14th (150) vs 13th (100) -> +50%, NOT touching the 999.
    assert view.revenue_delta is not None
    assert view.revenue_delta.state == "up"
    assert view.revenue_delta.pct == Decimal("50.0")


def test_peak_is_highest_revenue_bucket_and_none_when_empty():
    with SessionLocal() as db:
        _seed(db, date(2026, 5, 10), gross=100, units=1)
        _seed(db, date(2026, 5, 11), gross=300, units=1)
        db.commit()
        view = compute_sales_report(db, "daily", as_of=date(2026, 5, 12))
    assert view.peak is not None and view.peak.key == "2026-05-11"

    # Reset to a truly empty DB for the second half of this test.
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        empty = compute_sales_report(db, "daily", as_of=date(2026, 5, 12))
    assert empty.peak is None
    assert empty.total_revenue == Decimal("0.00")
    # All-zero window: no prior bucket had orders, so compute_delta returns
    # state='new' (not None) — still means "no meaningful trend signal".
    # All-zero window: prior bucket has no orders → compute_delta returns "new"
    assert empty.revenue_delta is not None and empty.revenue_delta.state == "new"
    assert len(empty.buckets) == 30          # window still fully seeded with zeros


def test_monthly_revenue_ties_to_monthly_pnl_gmv():
    """The page's headline claim: monthly sales revenue == MonthlyPnL.gmv."""
    from app.reports.monthly_pnl import compute_monthly_pnl
    with SessionLocal() as db:
        _seed(db, date(2026, 5, 8), gross=200, units=2, shipping_revenue=15,
              seller_funded_outlandish=10, platform_discount_total=8)
        _seed(db, date(2026, 5, 18), gross=120, units=1, seller_funded_smashbox=6,
              payment_platform_discount=3)
        db.commit()
        view = compute_sales_report(db, "monthly", as_of=date(2026, 5, 25))
        may = next(b for b in view.buckets if b.key == "2026-05")
        pnl = compute_monthly_pnl(db, 2026, 5)
    assert may.revenue == pnl.gmv


def test_weekly_rolls_up_within_iso_week():
    from datetime import timedelta
    ref = date(2026, 5, 15)
    monday = ref - timedelta(days=ref.weekday())     # Monday of ref's ISO week
    with SessionLocal() as db:
        _seed(db, monday, gross=100, units=1)
        _seed(db, monday + timedelta(days=2), gross=50, units=2)   # same ISO week
        db.commit()
        view = compute_sales_report(db, "weekly", as_of=ref)
    wk = next(b for b in view.buckets if b.key == monday.isoformat())
    assert wk.revenue == Decimal("150.00")   # 100 + 50
    assert wk.units == 3                       # 1 + 2
    assert wk.orders == 2


def test_custom_range_limits_buckets_daily():
    with SessionLocal() as db:
        _seed(db, date(2026, 3, 10), gross=100, units=1)
        _seed(db, date(2026, 5, 20), gross=50, units=1)   # outside the range
        db.commit()
        view = compute_sales_report(db, "daily",
                                    start=date(2026, 3, 1), end=date(2026, 3, 31),
                                    as_of=date(2026, 6, 1))
    assert view.window_start == date(2026, 3, 1)
    assert view.window_end == date(2026, 3, 31)
    keys = [b.key for b in view.buckets]
    assert "2026-03-10" in keys
    assert "2026-05-20" not in keys
    assert view.total_revenue == Decimal("100.00")


def test_custom_range_weekly_buckets_the_span():
    with SessionLocal() as db:
        _seed(db, date(2026, 3, 3), gross=100, units=1)
        _seed(db, date(2026, 3, 17), gross=40, units=1)
        db.commit()
        view = compute_sales_report(db, "weekly",
                                    start=date(2026, 3, 1), end=date(2026, 3, 28),
                                    as_of=date(2026, 6, 1))
    assert view.total_revenue == Decimal("140.00")
    assert all(b.start.weekday() == 0 for b in view.buckets)   # weekly = Mondays
