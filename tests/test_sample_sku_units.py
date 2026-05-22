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
from app.models.sku_alias import SkuAlias
from app.reports.sample_tracking import (
    SamplePeriodKind,
    compute_sample_view,
    count_sku_units_shipped,
    samples_by_sku_shipped,
    samples_vs_sales_by_sku,
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


# ---- Alias consolidation -------------------------------------------------
# Pre-fix bug: a SKU re-coded mid-window (e.g. legacy 'LEGACY-A' aliased to
# canonical 'TT-A') showed up as TWO rows on the sample-tracking page because
# the aggregators grouped by raw OrderLine.sku / Sample.sku. The fix loads
# the alias map and canonicalizes keys before grouping.

def _alias(db, alias_sku: str, canonical_sku: str) -> None:
    db.add(SkuAlias(alias_sku=alias_sku, canonical_sku=canonical_sku))
    db.flush()


def test_samples_vs_sales_consolidates_aliased_keys():
    """Samples shipped under both an alias code and its canonical must
    collapse to one row in samples_vs_sales_by_sku."""
    with SessionLocal() as db:
        db.add(Sku(sku="A", tiktok_sku_id="TT-A", name="Product A",
                   brand="b", unit_cogs=Decimal("1")))
        db.flush()
        _alias(db, "LEGACY-A", "TT-A")

        batch = _batch(db)
        # 3 sample units under the legacy code, 5 under the canonical.
        _sample_order_line(db, batch, tiktok_oid="O-OLD", sku="LEGACY-A", quantity=3)
        _sample_order_line(db, batch, tiktok_oid="O-NEW", sku="TT-A", quantity=5)
        db.commit()

        rows = samples_vs_sales_by_sku(db, datetime(2026, 5, 1), datetime(2026, 6, 1))

    # ONE row, not two; total samples = 3 + 5 = 8.
    assert len(rows) == 1
    assert rows[0].tiktok_sku_id == "TT-A"
    assert rows[0].samples_sent == 8


def test_samples_by_sku_shipped_consolidates_aliased_keys():
    """Same consolidation guarantee for the dashboard 'Samples sent by SKU' table."""
    with SessionLocal() as db:
        db.add(Sku(sku="A", tiktok_sku_id="TT-A", name="Product A",
                   brand="b", unit_cogs=Decimal("1")))
        db.flush()
        _alias(db, "LEGACY-A", "TT-A")

        batch = _batch(db)
        _sample_order_line(db, batch, tiktok_oid="O-OLD", sku="LEGACY-A", quantity=3)
        _sample_order_line(db, batch, tiktok_oid="O-NEW", sku="TT-A", quantity=5)
        # Manual Sample row under the legacy code too — must also collapse.
        db.add(Sample(
            import_batch_id=batch.id,
            shipped_at=datetime(2026, 5, 10),
            sku="LEGACY-A",
            quantity=2,
        ))
        db.commit()

        rows = samples_by_sku_shipped(db, datetime(2026, 5, 1), datetime(2026, 6, 1))

    assert len(rows) == 1
    assert rows[0].tiktok_sku_id == "TT-A"
    assert rows[0].samples_sent == 10  # 3 (order) + 5 (order) + 2 (Sample table)


def test_count_sku_units_shipped_consolidates_aliased_bundle_keys():
    """A bundle re-listed under a new TikTok ID — sample units under the old
    bundle code should expand through the canonical bundle's components."""
    with SessionLocal() as db:
        # Bundle catalog row keyed by the canonical TikTok ID.
        bundle = Bundle(tiktok_sku_id="TT-BUNDLE-NEW", bundle_sku="BUNDLE",
                        name="duo", brand="b")
        db.add(bundle)
        db.flush()
        db.add(BundleComponent(bundle_id=bundle.id, component_sku="X",
                               quantity=1, unit_cogs=Decimal("1")))
        db.add(BundleComponent(bundle_id=bundle.id, component_sku="Y",
                               quantity=1, unit_cogs=Decimal("1")))

        # Old bundle TikTok ID aliased to new.
        _alias(db, "TT-BUNDLE-OLD", "TT-BUNDLE-NEW")

        batch = _batch(db)
        # 1 sample under old bundle code, 1 under new — both must expand to
        # 2 component units each, total = 4.
        _sample_order_line(db, batch, tiktok_oid="O-OLD", sku="TT-BUNDLE-OLD", quantity=1)
        _sample_order_line(db, batch, tiktok_oid="O-NEW", sku="TT-BUNDLE-NEW", quantity=1)
        db.commit()

        total = count_sku_units_shipped(db, datetime(2026, 5, 1), datetime(2026, 6, 1))

    # Both line qtys collapse to canonical TT-BUNDLE-NEW (combined qty=2),
    # then expand via the 2-component bundle → 2 × 2 = 4.
    assert total == 4


def test_alias_map_explicit_empty_disables_collapse():
    """Passing alias_map={} short-circuits the DB lookup so callers can run
    pre-aliased analyses (e.g. for diff'ing before/after a merge)."""
    with SessionLocal() as db:
        db.add(Sku(sku="A", tiktok_sku_id="TT-A", name="Product A",
                   brand="b", unit_cogs=Decimal("1")))
        db.flush()
        _alias(db, "LEGACY-A", "TT-A")

        batch = _batch(db)
        _sample_order_line(db, batch, tiktok_oid="O-OLD", sku="LEGACY-A", quantity=3)
        _sample_order_line(db, batch, tiktok_oid="O-NEW", sku="TT-A", quantity=5)
        db.commit()

        # Default: alias collapses to one row.
        with_aliases = samples_vs_sales_by_sku(
            db, datetime(2026, 5, 1), datetime(2026, 6, 1)
        )
        assert len(with_aliases) == 1

        # Explicit empty: two raw rows again.
        without_aliases = samples_vs_sales_by_sku(
            db, datetime(2026, 5, 1), datetime(2026, 6, 1), alias_map={}
        )
        assert len(without_aliases) == 2
        skus = {r.tiktok_sku_id for r in without_aliases}
        assert skus == {"LEGACY-A", "TT-A"}
