"""Bundle explosion in compute_sku_profitability.

Asserts the allocation invariants:
  - components share the bundle's gross EXACTLY (sum across components = bundle gross)
  - components share the bundle's COGS EXACTLY
  - units = bundle units × component quantity
  - direct component sales merge with bundle-allocated rows (same physical SKU)
"""
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
    Sku,
)
from app.reports.sku_profitability import compute_sku_profitability


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


def _order_with_line(db, batch, *, tiktok_oid, sku, quantity, gross, cogs_snapshot):
    o = Order(
        import_batch_id=batch.id,
        tiktok_order_id=tiktok_oid,
        placed_at=datetime(2026, 5, 5),
        order_type=OrderType.PAID,
        status="Completed",
        brand="smashbox",
    )
    db.add(o)
    db.flush()
    db.add(OrderLine(
        order_id=o.id,
        sku=sku,
        quantity=quantity,
        gross_sales=Decimal(gross),
        unit_cogs_snapshot=Decimal(cogs_snapshot),
    ))


def _seed_two_components(db):
    """Two physical SKUs (A, B) plus a 2-pack bundle containing 1 of each."""
    db.add(Sku(sku="A", tiktok_sku_id="TT-A", name="Item A", brand="b", unit_cogs=Decimal("3")))
    db.add(Sku(sku="B", tiktok_sku_id="TT-B", name="Item B", brand="b", unit_cogs=Decimal("7")))
    db.flush()

    bundle = Bundle(
        tiktok_sku_id="TT-BUNDLE",
        bundle_sku="A-BUNDLE",
        name="A+B Pack",
        brand="b",
    )
    db.add(bundle)
    db.flush()
    db.add(BundleComponent(bundle_id=bundle.id, component_sku="A", quantity=1, unit_cogs=Decimal("3")))
    db.add(BundleComponent(bundle_id=bundle.id, component_sku="B", quantity=1, unit_cogs=Decimal("7")))


def test_bundle_gross_and_cogs_split_proportionally_to_cogs():
    """A bundle priced at $100 with components of $3 / $7 COGS → A gets 30%, B gets 70%."""
    with SessionLocal() as db:
        _seed_two_components(db)
        batch = _batch(db)
        _order_with_line(
            db, batch,
            tiktok_oid="O-1", sku="TT-BUNDLE", quantity=1,
            gross="100.00", cogs_snapshot="10",  # 3+7
        )
        db.commit()

        rows = compute_sku_profitability(db, datetime(2026, 5, 1), datetime(2026, 6, 1))
        by_key = {r.tiktok_sku_id: r for r in rows}

    # Bundle row should be gone — exploded.
    assert "TT-BUNDLE" not in by_key
    assert "TT-A" in by_key and "TT-B" in by_key

    a = by_key["TT-A"]
    b = by_key["TT-B"]
    assert a.units_sold == 1 and b.units_sold == 1
    assert a.gross_sales == Decimal("30.00")
    assert b.gross_sales == Decimal("70.00")
    assert a.cogs == Decimal("3")
    assert b.cogs == Decimal("7")
    # Invariant: across components, gross/cogs sum back to the bundle's values
    assert a.gross_sales + b.gross_sales == Decimal("100.00")
    assert a.cogs + b.cogs == Decimal("10")


def test_bundle_units_scale_by_line_quantity_and_component_quantity():
    """Selling 4 of a bundle that contains 2× component A → A gets 8 units."""
    with SessionLocal() as db:
        db.add(Sku(sku="A", tiktok_sku_id="TT-A", name="A", brand="b", unit_cogs=Decimal("5")))
        db.flush()
        bundle = Bundle(tiktok_sku_id="TT-BUNDLE", bundle_sku="A-BUNDLE", name="2pk A", brand="b")
        db.add(bundle)
        db.flush()
        db.add(BundleComponent(bundle_id=bundle.id, component_sku="A", quantity=2, unit_cogs=Decimal("5")))

        batch = _batch(db)
        _order_with_line(
            db, batch,
            tiktok_oid="O-1", sku="TT-BUNDLE", quantity=4,
            gross="200.00", cogs_snapshot="10",  # 2 × 5
        )
        db.commit()

        rows = compute_sku_profitability(db, datetime(2026, 5, 1), datetime(2026, 6, 1))

    assert len(rows) == 1
    a = rows[0]
    assert a.tiktok_sku_id == "TT-A"
    assert a.units_sold == 8                          # 4 bundles × 2 each
    assert a.gross_sales == Decimal("200.00")
    assert a.cogs == Decimal("40")                    # 4 × 2 × $5


def test_direct_sale_and_bundle_allocation_merge():
    """A component that ALSO sells directly should land in one merged row."""
    with SessionLocal() as db:
        _seed_two_components(db)
        batch = _batch(db)
        # Direct sale of A
        _order_with_line(
            db, batch,
            tiktok_oid="O-DIRECT", sku="TT-A", quantity=2,
            gross="20.00", cogs_snapshot="3",
        )
        # Bundle containing A
        _order_with_line(
            db, batch,
            tiktok_oid="O-BUNDLE", sku="TT-BUNDLE", quantity=1,
            gross="100.00", cogs_snapshot="10",
        )
        db.commit()

        rows = compute_sku_profitability(db, datetime(2026, 5, 1), datetime(2026, 6, 1))
        by_key = {r.tiktok_sku_id: r for r in rows}

    a = by_key["TT-A"]
    assert a.units_sold == 3                          # 2 direct + 1 from bundle
    assert a.gross_sales == Decimal("50.00")          # $20 direct + $30 bundle share
    assert a.cogs == Decimal("9")                     # 2×$3 direct + 1×$3 bundle


def test_bundle_falls_back_to_msrp_when_cogs_zero():
    """Bundle with zero COGS basis allocates by MSRP instead."""
    with SessionLocal() as db:
        db.add(Sku(sku="A", tiktok_sku_id="TT-A", name="A", brand="b", unit_cogs=Decimal("0")))
        db.add(Sku(sku="B", tiktok_sku_id="TT-B", name="B", brand="b", unit_cogs=Decimal("0")))
        db.flush()
        bundle = Bundle(tiktok_sku_id="TT-BUNDLE", bundle_sku="A-BUNDLE", name="Pack", brand="b")
        db.add(bundle)
        db.flush()
        db.add(BundleComponent(bundle_id=bundle.id, component_sku="A", quantity=1, msrp=Decimal("25"), unit_cogs=Decimal("0")))
        db.add(BundleComponent(bundle_id=bundle.id, component_sku="B", quantity=1, msrp=Decimal("75"), unit_cogs=Decimal("0")))

        batch = _batch(db)
        _order_with_line(
            db, batch,
            tiktok_oid="O-1", sku="TT-BUNDLE", quantity=1,
            gross="100.00", cogs_snapshot="0",
        )
        db.commit()

        rows = compute_sku_profitability(db, datetime(2026, 5, 1), datetime(2026, 6, 1))
        by_key = {r.tiktok_sku_id: r for r in rows}

    # MSRP-based: 25/100 to A, 75/100 to B
    assert by_key["TT-A"].gross_sales == Decimal("25.00")
    assert by_key["TT-B"].gross_sales == Decimal("75.00")


def test_unmapped_bundle_with_no_basis_is_kept_as_is():
    """A bundle with zero COGS AND zero MSRP can't be exploded — keep as one row."""
    with SessionLocal() as db:
        bundle = Bundle(tiktok_sku_id="TT-BUNDLE", bundle_sku="X-BUNDLE", name="Empty bundle", brand="b")
        db.add(bundle)
        db.flush()
        db.add(BundleComponent(bundle_id=bundle.id, component_sku="X", quantity=1, msrp=Decimal("0"), unit_cogs=Decimal("0")))

        batch = _batch(db)
        _order_with_line(
            db, batch,
            tiktok_oid="O-1", sku="TT-BUNDLE", quantity=1,
            gross="50.00", cogs_snapshot="0",
        )
        db.commit()

        rows = compute_sku_profitability(db, datetime(2026, 5, 1), datetime(2026, 6, 1))

    assert len(rows) == 1
    assert rows[0].tiktok_sku_id == "TT-BUNDLE"
    assert rows[0].is_bundle is True
    assert rows[0].gross_sales == Decimal("50.00")


def test_results_sorted_by_gross_desc():
    """After explosion, the final list is still sorted by gross descending."""
    with SessionLocal() as db:
        _seed_two_components(db)
        batch = _batch(db)
        _order_with_line(
            db, batch,
            tiktok_oid="O-1", sku="TT-BUNDLE", quantity=1,
            gross="100.00", cogs_snapshot="10",
        )
        db.commit()

        rows = compute_sku_profitability(db, datetime(2026, 5, 1), datetime(2026, 6, 1))

    assert [r.gross_sales for r in rows] == sorted(
        [r.gross_sales for r in rows], reverse=True
    )
