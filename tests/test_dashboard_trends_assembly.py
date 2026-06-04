"""DB-backed tests for build_dashboard_trends — the route-level assembly that
turns a trailing run of MonthlyPnL into per-KPI deltas + sparkline series.

Focus (per spec): the prior_has_data branch end-to-end on real data —
  - a month WITH a populated prior month -> a real up/down delta
  - a month with NO genuine prior (early data) -> "new", never an error
  - with_delta=False (aggregate views) -> deltas suppressed, sparklines kept
"""
from datetime import datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import (
    ImportBatch,
    ImportBatchStatus,
    ImportFileKind,
    Order,
    OrderLine,
    OrderType,
)
from app.reports.dashboard_trends import MetricTrend, build_dashboard_trends


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _batch(db) -> ImportBatch:
    b = ImportBatch(
        kind=ImportFileKind.TIKTOK_ORDERS,
        status=ImportBatchStatus.COMPLETED,
        original_filename="t.csv",
        stored_path="/tmp/t.csv",
    )
    db.add(b)
    db.flush()
    return b


def _paid_order(db, batch_id, placed_at, *, tt_id, gross_sales):
    o = Order(
        import_batch_id=batch_id,
        tiktok_order_id=tt_id,
        placed_at=placed_at,
        order_type=OrderType.PAID,
        status="Shipped",
        brand="smashbox",
        gross_sales=Decimal(gross_sales),
        tiktok_fees=Decimal("10.00"),
        tiktok_referral_fee=Decimal("10.00"),
    )
    db.add(o)
    db.flush()
    db.add(OrderLine(
        order_id=o.id, sku="SBX-001", quantity=1,
        unit_price=Decimal(gross_sales), gross_sales=Decimal(gross_sales),
        unit_cogs_snapshot=Decimal("3.00"),
    ))
    db.flush()
    return o


def test_assembly_computes_real_delta_when_prior_month_has_data():
    db = SessionLocal()
    try:
        b = _batch(db)
        _paid_order(db, b.id, datetime(2026, 2, 10), tt_id="F1", gross_sales="100")
        _paid_order(db, b.id, datetime(2026, 3, 10), tt_id="M1", gross_sales="200")
        db.commit()

        trends = build_dashboard_trends(db, 2026, 3, with_delta=True)
        np = trends["net_profit"]
        assert isinstance(np, MetricTrend)
        # March net profit (~187) > February (~87) -> a genuine "up" delta.
        assert np.delta is not None
        assert np.delta.state == "up"
        assert np.delta.label.startswith("+")
        assert np.spark != ""          # multi-month series draws a line

        # Per-metric mode wiring: margin must be percentage-POINTS (pp), not %.
        assert trends["gross_margin"].delta.label.endswith("pp")

        # P&L-page metrics: gross_sales/gross_profit (relative %), net_margin (pp).
        assert {"gross_sales", "gross_profit", "net_margin"}.issubset(trends.keys())
        assert trends["gross_sales"].delta.label.endswith("%")
        assert trends["gross_profit"].delta.label.endswith("%")
        assert trends["net_margin"].delta.label.endswith("pp")
    finally:
        db.close()


def test_assembly_missing_prior_renders_new_not_error():
    db = SessionLocal()
    try:
        b = _batch(db)
        # Only March exists — February (the prior month) has no activity.
        _paid_order(db, b.id, datetime(2026, 3, 10), tt_id="M1", gross_sales="200")
        db.commit()

        trends = build_dashboard_trends(db, 2026, 3, with_delta=True)
        np = trends["net_profit"]
        assert np.delta is not None
        assert np.delta.state == "new"
        assert np.delta.label == "new"
        assert np.delta.pct is None
    finally:
        db.close()


def test_assembly_suppresses_deltas_when_with_delta_false():
    db = SessionLocal()
    try:
        b = _batch(db)
        _paid_order(db, b.id, datetime(2026, 2, 10), tt_id="F1", gross_sales="100")
        _paid_order(db, b.id, datetime(2026, 3, 10), tt_id="M1", gross_sales="200")
        db.commit()

        trends = build_dashboard_trends(db, 2026, 3, with_delta=False)
        np = trends["net_profit"]
        assert np.delta is None        # aggregate view: no MoM chip
        assert np.spark != ""          # but the sparkline still renders
    finally:
        db.close()
