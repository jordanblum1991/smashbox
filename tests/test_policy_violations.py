"""Tests for the policy-violations report."""
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
    Sku,
)
from app.reports.pnl import PeriodKind
from app.reports.policy_violations import (
    all_policy_violations,
    compute_policy_violations,
    count_policy_violations,
)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _batch(db):
    b = ImportBatch(
        kind=ImportFileKind.TIKTOK_ORDERS,
        status=ImportBatchStatus.COMPLETED,
        original_filename="x",
        stored_path="x",
    )
    db.add(b)
    db.flush()
    return b


def _seed(db, *, placed_at, gross, seller_funded, violates, sku="SBX-A", order_type=OrderType.PAID, status="Completed"):
    o = Order(
        import_batch_id=_batch(db).id,
        tiktok_order_id=f"O-{placed_at.isoformat()}-{sku}-{seller_funded}",
        placed_at=placed_at,
        order_type=order_type,
        status=status,
        brand="smashbox",
    )
    db.add(o)
    db.flush()
    db.add(OrderLine(
        order_id=o.id,
        sku=sku,
        quantity=1,
        gross_sales=Decimal(gross),
        seller_funded_discount=Decimal(seller_funded),
        discount_policy_violation=violates,
    ))


def test_all_policy_violations_all_time_and_acknowledged_filter():
    with SessionLocal() as db:
        _seed(db, placed_at=datetime(2026, 5, 10), gross="100", seller_funded="35", violates=True)
        _seed(db, placed_at=datetime(2026, 1, 3), gross="100", seller_funded="40", violates=True, sku="SBX-B")
        _seed(db, placed_at=datetime(2026, 5, 11), gross="100", seller_funded="20", violates=False)  # compliant
        db.commit()
        rows = all_policy_violations(db, only_unacknowledged=True)
        assert len(rows) == 2                          # both flagged, across months; compliant excluded
        assert rows[0].placed_at >= rows[1].placed_at  # most recent first
        # Acknowledge one → it drops from the unacknowledged list but stays in the full list.
        line = db.query(OrderLine).filter(OrderLine.discount_policy_violation.is_(True)).first()
        line.policy_violation_acknowledged = True
        db.commit()
        assert len(all_policy_violations(db, only_unacknowledged=True)) == 1
        assert len(all_policy_violations(db, only_unacknowledged=False)) == 2


def test_returns_only_flagged_lines_in_period():
    """Compliant lines + out-of-period lines must not appear."""
    with SessionLocal() as db:
        # Flagged, in period — INCLUDED
        _seed(db, placed_at=datetime(2026, 5, 10), gross="100", seller_funded="35", violates=True)
        # Compliant, in period — excluded
        _seed(db, placed_at=datetime(2026, 5, 11), gross="100", seller_funded="20", violates=False)
        # Flagged, but in April — excluded by period filter
        _seed(db, placed_at=datetime(2026, 4, 28), gross="100", seller_funded="40", violates=True)
        db.commit()

        view = compute_policy_violations(db, PeriodKind.MONTH, year=2026, month=5)

    assert len(view.rows) == 1
    assert view.rows[0].seller_funded_discount == Decimal("35")


def test_violation_math_is_correct():
    """cap = gross × 0.30; excess = seller_funded − cap; pct = seller_funded / gross."""
    with SessionLocal() as db:
        _seed(db, placed_at=datetime(2026, 5, 5), gross="100", seller_funded="45", violates=True)
        db.commit()

        view = compute_policy_violations(db, PeriodKind.MONTH, year=2026, month=5)

    r = view.rows[0]
    assert r.cap_amount == Decimal("30.00")
    assert r.excess == Decimal("15.00")
    assert r.pct_of_msrp == Decimal("45") / Decimal("100")


def test_aggregate_tiles_total_excess_and_affected_orders():
    with SessionLocal() as db:
        # Mid-day timestamps so both orders bucket into May under shop-local
        # (Pacific) reporting — a midnight-of-the-1st here would belong to April
        # in Pacific terms (see test_reporting_tz). This test is about excess
        # aggregation, not the period boundary.
        _seed(db, placed_at=datetime(2026, 5, 1, 12, 0), gross="100", seller_funded="50", violates=True, sku="A")  # excess $20
        _seed(db, placed_at=datetime(2026, 5, 2, 12, 0), gross="200", seller_funded="80", violates=True, sku="B")  # excess $20
        db.commit()

        view = compute_policy_violations(db, PeriodKind.MONTH, year=2026, month=5)

    assert view.total_excess == Decimal("40.00")
    assert view.total_seller_funded == Decimal("130")
    assert view.affected_orders == 2


def test_sample_orders_excluded():
    """Only PAID order lines are surfaced — sample orders bypass the report."""
    with SessionLocal() as db:
        _seed(db, placed_at=datetime(2026, 5, 5), gross="100", seller_funded="40", violates=True, order_type=OrderType.SAMPLE)
        db.commit()

        view = compute_policy_violations(db, PeriodKind.MONTH, year=2026, month=5)

    assert view.rows == []


def test_catalog_enrichment_supplies_name_and_code():
    with SessionLocal() as db:
        db.add(Sku(sku="SBX-A", tiktok_sku_id="TT-A", name="Test Lipstick", brand="smashbox"))
        db.flush()
        _seed(db, placed_at=datetime(2026, 5, 5), gross="100", seller_funded="50", violates=True, sku="TT-A")
        db.commit()

        view = compute_policy_violations(db, PeriodKind.MONTH, year=2026, month=5)

    assert view.rows[0].name == "Test Lipstick"
    assert view.rows[0].sku_code == "SBX-A"
    assert view.rows[0].is_bundle is False


def test_ytd_period_includes_prior_months():
    with SessionLocal() as db:
        _seed(db, placed_at=datetime(2026, 2, 15), gross="100", seller_funded="40", violates=True, sku="A")
        _seed(db, placed_at=datetime(2026, 5, 15), gross="100", seller_funded="45", violates=True, sku="B")
        db.commit()

        view = compute_policy_violations(db, PeriodKind.YTD, year=2026, month=5)

    assert len(view.rows) == 2


def test_count_all_time_used_by_nav():
    with SessionLocal() as db:
        _seed(db, placed_at=datetime(2025, 12, 1), gross="100", seller_funded="40", violates=True, sku="A")
        _seed(db, placed_at=datetime(2026, 5, 5), gross="100", seller_funded="45", violates=True, sku="B")
        _seed(db, placed_at=datetime(2026, 5, 5), gross="100", seller_funded="20", violates=False, sku="C")
        db.commit()

        assert count_policy_violations(db) == 2
