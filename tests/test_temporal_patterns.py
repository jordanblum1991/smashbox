# tests/test_temporal_patterns.py
"""Aggregate temporal patterns: revenue by local weekday/hour/date, day-of-week
avg-per-occurrence, trend-shape classification, insights. Buckets are derived
through placed_local() (the same shop-local conversion the report uses), so the
tests assert against placed_local-derived expectations rather than hardcoding the
DST offset."""
import itertools
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.order import Order, OrderType
from app.reports.temporal_patterns import compute_temporal_patterns
from app.services.reporting_tz import placed_local

_OID = itertools.count(1)
WSTART, WEND = date(2026, 5, 1), date(2026, 5, 31)   # 31-day window


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _order(db, dt, rev, order_type=OrderType.PAID):
    """A PAID order at placed_at=dt whose canonical GMV equals `rev` (all the
    other revenue components are zeroed so the SQL sum is non-NULL)."""
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                    original_filename="t", stored_path="t")
    db.add(b); db.flush()
    db.add(Order(import_batch_id=b.id, tiktok_order_id=f"O{next(_OID)}", placed_at=dt,
                 order_type=order_type, status="Completed", brand="smashbox",
                 gross_sales=Decimal(str(rev)), shipping_revenue=Decimal("0"),
                 seller_funded_outlandish=Decimal("0"), seller_funded_smashbox=Decimal("0"),
                 platform_discount_total=Decimal("0"), payment_platform_discount=Decimal("0")))
    db.flush()


def test_revenue_buckets_to_local_weekday_and_hour():
    dt = datetime(2026, 5, 20, 14, 0)
    with SessionLocal() as db:
        _order(db, dt, 100); db.commit()
        v = compute_temporal_patterns(db, start=WSTART, end=WEND)
    exp_wd, exp_h = placed_local(dt).weekday(), placed_local(dt).hour
    assert v.dow[exp_wd].revenue == Decimal("100.00")
    assert v.hours[exp_h].revenue == Decimal("100.00")
    assert v.hours[exp_h].is_peak
    assert v.total_revenue == Decimal("100.00")
    assert sum((d.revenue for d in v.dow), Decimal("0")) == Decimal("100.00")


def test_avg_per_occurrence():
    dt1, dt2 = datetime(2026, 5, 6, 12, 0), datetime(2026, 5, 13, 12, 0)  # same weekday, +7d
    with SessionLocal() as db:
        _order(db, dt1, 100); _order(db, dt2, 100); db.commit()
        v = compute_temporal_patterns(db, start=WSTART, end=WEND)
    wd = placed_local(dt1).weekday()
    assert placed_local(dt2).weekday() == wd
    stat = v.dow[wd]
    assert stat.revenue == Decimal("200.00")
    assert stat.occurrences >= 2
    assert stat.avg_revenue == (Decimal("200.00") / stat.occurrences).quantize(Decimal("0.01"))
    assert stat.is_peak


def test_peak_weekday_and_insights():
    sat, mon = datetime(2026, 5, 9, 12, 0), datetime(2026, 5, 11, 12, 0)
    with SessionLocal() as db:
        _order(db, sat, 500); _order(db, mon, 100); db.commit()
        v = compute_temporal_patterns(db, start=WSTART, end=WEND)
    wd_sat = placed_local(sat).weekday()
    assert v.dow[wd_sat].is_peak
    assert v.insights.strongest_dow.weekday == wd_sat
    assert v.insights.peak_hour is not None
    assert v.insights.peak_hour_range is not None
    assert v.insights.best_day is not None
    assert v.insights.best_day.revenue == Decimal("500.00")


def test_trend_up():
    base = date(2026, 5, 1)
    with SessionLocal() as db:
        for i in range(16):
            d = base + timedelta(days=i)
            _order(db, datetime(d.year, d.month, d.day, 12, 0), 10 + i * 10)
        db.commit()
        v = compute_temporal_patterns(db, start=base, end=base + timedelta(days=15))
    assert v.insights.trend.has_enough
    assert v.insights.trend.label == "Trending up"
    assert v.insights.trend.direction == "up"


def test_trend_steady():
    base = date(2026, 5, 1)
    with SessionLocal() as db:
        for i in range(14):
            d = base + timedelta(days=i)
            _order(db, datetime(d.year, d.month, d.day, 12, 0), 100)
        db.commit()
        v = compute_temporal_patterns(db, start=base, end=base + timedelta(days=13))
    assert v.insights.trend.label == "Steady"
    assert v.insights.trend.direction == "flat"
    assert v.insights.trend.volatility == "steady"


def test_trend_spiky():
    base = date(2026, 5, 1)
    vals = [0, 0, 500, 0, 0, 0, 0, 0, 0, 500, 0, 0, 0, 0]  # a burst in each half → flat dir, high CV
    with SessionLocal() as db:
        for i, val in enumerate(vals):
            if val:
                d = base + timedelta(days=i)
                _order(db, datetime(d.year, d.month, d.day, 12, 0), val)
        db.commit()
        v = compute_temporal_patterns(db, start=base, end=base + timedelta(days=len(vals) - 1))
    assert v.insights.trend.volatility == "spiky"
    assert v.insights.trend.label == "Spiky"


def test_dayparts_sum_hours():
    dt = datetime(2026, 5, 20, 14, 0)
    with SessionLocal() as db:
        _order(db, dt, 250); db.commit()
        v = compute_temporal_patterns(db, start=WSTART, end=WEND)
    h = placed_local(dt).hour
    key = ("morning" if 5 <= h < 12 else "afternoon" if 12 <= h < 17
           else "evening" if 17 <= h < 22 else "night")
    dp = {d.key: d for d in v.dayparts}[key]
    assert dp.revenue == Decimal("250.00")
    assert dp.is_peak


def test_paid_only_and_empty():
    with SessionLocal() as db:
        _order(db, datetime(2026, 5, 20, 12, 0), 999, order_type=OrderType.SAMPLE)
        db.commit()
        v = compute_temporal_patterns(db, start=WSTART, end=WEND)
    assert v.total_revenue == Decimal("0.00")
    assert v.insights.strongest_dow is None
    assert v.insights.peak_hour is None
    assert v.insights.best_day is None
    assert not v.insights.trend.has_enough
    assert v.top_days == []
