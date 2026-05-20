"""count_sku_units_shipped: bundles expand to component counts."""
from datetime import datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import (
    Bundle,
    BundleComponent,
    ImportBatch,
    ImportBatchStatus,
    ImportFileKind,
    Order,
    OrderLine,
    OrderType,
    Sample,
    Sku,
)
from app.reports.sample_tracking import compute_sample_view, count_sku_units_shipped, SamplePeriodKind


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


def _sample_order_line(db, batch, *, tiktok_oid, sku, quantity):
    o = Order(
        import_batch_id=batch.id,
        tiktok_order_id=tiktok_oid,
        placed_at=datetime(2026, 5, 5),
        order_type=OrderType.SAMPLE,
        status="Shipped",
        brand="smashbox",
    )
    db.add(o)
    db.flush()
    db.add(OrderLine(order_id=o.id, sku=sku, quantity=quantity, gross_sales=Decimal("0")))


def test_bundle_lines_expand_to_component_count():
    """Bundle of 3 components, sample line qty=2 → 6 expanded units. Plus a single
    SKU line qty=3 → +3. Plus a manual Sample row qty=4 for a single SKU → +4.
    Total = 13."""
    with SessionLocal() as db:
        db.add(Sku(sku="A", tiktok_sku_id="TT-A", name="A", brand="b", unit_cogs=Decimal("1")))
        db.flush()

        bundle = Bundle(tiktok_sku_id="TT-BUNDLE", bundle_sku="A-BUNDLE", name="3pack", brand="b")
        db.add(bundle)
        db.flush()
        # Components summing to 3 (1 + 1 + 1)
        db.add(BundleComponent(bundle_id=bundle.id, component_sku="A", quantity=1, unit_cogs=Decimal("1")))
        db.add(BundleComponent(bundle_id=bundle.id, component_sku="B", quantity=1, unit_cogs=Decimal("1")))
        db.add(BundleComponent(bundle_id=bundle.id, component_sku="C", quantity=1, unit_cogs=Decimal("1")))

        batch = _batch(db)
        _sample_order_line(db, batch, tiktok_oid="O-BUNDLE", sku="TT-BUNDLE", quantity=2)
        _sample_order_line(db, batch, tiktok_oid="O-SINGLE", sku="TT-A", quantity=3)
        db.add(Sample(
            import_batch_id=batch.id,
            shipped_at=datetime(2026, 5, 10),
            sku="TT-A",
            quantity=4,
        ))
        db.commit()

        total = count_sku_units_shipped(db, datetime(2026, 5, 1), datetime(2026, 6, 1))

    assert total == 13  # (2 * 3) + 3 + 4


def test_uneven_component_quantities():
    """Bundle with components qty 1, 1, 2 (sum=4). Order line qty=2 → 8 units."""
    with SessionLocal() as db:
        bundle = Bundle(tiktok_sku_id="TT-DUO", bundle_sku="DUO", name="duo+spare", brand="b")
        db.add(bundle)
        db.flush()
        db.add(BundleComponent(bundle_id=bundle.id, component_sku="X", quantity=1, unit_cogs=Decimal("1")))
        db.add(BundleComponent(bundle_id=bundle.id, component_sku="Y", quantity=1, unit_cogs=Decimal("1")))
        db.add(BundleComponent(bundle_id=bundle.id, component_sku="Z", quantity=2, unit_cogs=Decimal("1")))

        batch = _batch(db)
        _sample_order_line(db, batch, tiktok_oid="O-1", sku="TT-DUO", quantity=2)
        db.commit()

        total = count_sku_units_shipped(db, datetime(2026, 5, 1), datetime(2026, 6, 1))

    assert total == 8  # 2 lines × 4 components-per-bundle


def test_unmapped_sku_counts_as_one_per_unit():
    """SKU with no Bundle row contributes raw quantity."""
    with SessionLocal() as db:
        batch = _batch(db)
        _sample_order_line(db, batch, tiktok_oid="O-1", sku="UNKNOWN", quantity=5)
        db.commit()

        total = count_sku_units_shipped(db, datetime(2026, 5, 1), datetime(2026, 6, 1))

    assert total == 5


def test_view_exposes_total_sku_units_shipped():
    """The SampleView dataclass surfaces the expanded total alongside total_units_shipped."""
    with SessionLocal() as db:
        bundle = Bundle(tiktok_sku_id="TT-BUNDLE", bundle_sku="BUNDLE", name="3pk", brand="b")
        db.add(bundle)
        db.flush()
        db.add(BundleComponent(bundle_id=bundle.id, component_sku="A", quantity=3, unit_cogs=Decimal("1")))

        batch = _batch(db)
        _sample_order_line(db, batch, tiktok_oid="O-1", sku="TT-BUNDLE", quantity=2)
        db.commit()

        view = compute_sample_view(db, SamplePeriodKind.MONTH, year=2026, month=5)

    assert view.total_units_shipped == 2          # order-line units (legacy meaning)
    assert view.total_sku_units_shipped == 6      # expanded: 2 × 3 components
