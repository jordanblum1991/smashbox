"""Tests for app/reports/sample_inventory.py and app/reports/samples_by_creator.py.

Covers:
  1. On-hand enrichment — mapped Sku row, unmapped key, bundle.
  2. Alias consolidation in sample_inventory view.
  3. Alias consolidation in samples_by_creator view (distinct SKU count).
  4. Creator grouping — normalized row, legacy row, one creator with BOTH
     (appears as two separate rows, legacy flagged).
  5. Cost suppression — shipping cost column hidden when all null, shown when any non-null.
"""
from datetime import datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.bundle import Bundle, BundleComponent
from app.models.creator import Creator
from app.models.sku import Sku
from app.models.sku_alias import SkuAlias
from app.reports.sample_inventory import compute_sample_inventory_view
from app.reports.samples_by_creator import compute_samples_by_creator_view
from app.services.sample_service import record_sample_receipt, record_sample_shipment


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _batch(db) -> ImportBatch:
    b = ImportBatch(
        kind=ImportFileKind.SAMPLES,
        status=ImportBatchStatus.COMPLETED,
        original_filename="test.csv",
        stored_path="/tmp/test.csv",
    )
    db.add(b)
    db.flush()
    return b


# ---------------------------------------------------------------------------
# 1. On-hand enrichment
# ---------------------------------------------------------------------------

def test_on_hand_mapped_sku():
    """Receipt + shipment against a Sku row → row carries sku_code and name."""
    with SessionLocal() as db:
        db.add(Sku(
            sku="SBX-001",
            name="Product One",
            brand="smashbox",
            tiktok_sku_id="SBX-001",
            unit_cogs=Decimal("5.00"),
        ))
        record_sample_receipt(
            db, sku="SBX-001", quantity=10,
            received_at=datetime(2026, 5, 1), brand="smashbox",
        )
        db.commit()

    with SessionLocal() as db:
        view = compute_sample_inventory_view(db)

    assert view.total_on_hand_units == 10
    assert view.sku_count == 1
    row = view.rows[0]
    assert row.sku_code == "SBX-001"
    assert row.name == "Product One"
    assert row.is_bundle is False
    assert row.on_hand_units == 10


def test_on_hand_unmapped_sku():
    """A ledger key with no Sku or Bundle row → sku_code=None, name=None."""
    with SessionLocal() as db:
        record_sample_receipt(
            db, sku="UNKNOWN-999", quantity=4,
            received_at=datetime(2026, 5, 1), brand="smashbox",
        )
        db.commit()

    with SessionLocal() as db:
        view = compute_sample_inventory_view(db)

    assert view.sku_count == 1
    row = view.rows[0]
    assert row.sku_code is None
    assert row.name is None
    assert row.on_hand_units == 4


def test_on_hand_bundle_sku():
    """A ledger key matched to a Bundle row → is_bundle=True, sku_code from bundle_sku."""
    with SessionLocal() as db:
        bundle = Bundle(
            bundle_sku="SBX-BUNDLE-1",
            name="Bundle One",
            tiktok_sku_id="SBX-BUNDLE-1",
            brand="smashbox",
        )
        db.add(bundle)
        db.flush()
        db.add(BundleComponent(bundle_id=bundle.id, component_sku="SBX-001", quantity=2))
        record_sample_receipt(
            db, sku="SBX-BUNDLE-1", quantity=5,
            received_at=datetime(2026, 5, 1), brand="smashbox",
        )
        db.commit()

    with SessionLocal() as db:
        view = compute_sample_inventory_view(db)

    assert view.sku_count == 1
    row = view.rows[0]
    assert row.is_bundle is True
    assert row.sku_code == "SBX-BUNDLE-1"
    assert row.name == "Bundle One"


def test_on_hand_mapped_before_unmapped():
    """Mapped rows sort by sku_code asc; unmapped rows appear after all mapped rows."""
    with SessionLocal() as db:
        db.add(Sku(
            sku="SBX-AAA", name="A Product", brand="smashbox",
            tiktok_sku_id="SBX-AAA", unit_cogs=Decimal("1.00"),
        ))
        record_sample_receipt(db, sku="ORPHAN-001", quantity=3,
                              received_at=datetime(2026, 5, 1), brand="smashbox")
        record_sample_receipt(db, sku="SBX-AAA", quantity=7,
                              received_at=datetime(2026, 5, 1), brand="smashbox")
        db.commit()

    with SessionLocal() as db:
        view = compute_sample_inventory_view(db)

    assert len(view.rows) == 2
    assert view.rows[0].sku_code == "SBX-AAA"   # mapped first
    assert view.rows[1].sku_code is None          # unmapped last


# ---------------------------------------------------------------------------
# 2. Alias consolidation — sample_inventory
# ---------------------------------------------------------------------------

def test_inventory_alias_consolidation():
    """Receipt under legacy code + shipment under canonical → single balance row."""
    legacy = "C001"
    canonical = "SBX-C001"

    with SessionLocal() as db:
        db.add(Sku(sku=canonical, name="C Product", brand="smashbox",
                   tiktok_sku_id=canonical, unit_cogs=Decimal("2.00")))
        record_sample_receipt(db, sku=legacy, quantity=8,
                              received_at=datetime(2026, 5, 1), brand="smashbox",
                              alias_map={})
        db.commit()

    with SessionLocal() as db:
        db.add(SkuAlias(alias_sku=legacy, canonical_sku=canonical))
        db.commit()

    with SessionLocal() as db:
        batch = _batch(db)
        record_sample_shipment(db, sku=canonical, quantity=3,
                               shipped_at=datetime(2026, 5, 2), brand="smashbox",
                               import_batch_id=batch.id)
        db.commit()

    with SessionLocal() as db:
        view = compute_sample_inventory_view(db)

    assert view.sku_count == 1
    assert view.total_on_hand_units == 5   # 8 − 3
    row = view.rows[0]
    assert row.canonical_sku == canonical
    assert row.sku_code == canonical


# ---------------------------------------------------------------------------
# 3. Alias consolidation — samples_by_creator (distinct SKU count)
# ---------------------------------------------------------------------------

def test_by_creator_alias_distinct_sku_count():
    """Two receipts under alias + canonical count as one distinct SKU in creator view."""
    legacy = "C002"
    canonical = "SBX-C002"

    with SessionLocal() as db:
        db.add(SkuAlias(alias_sku=legacy, canonical_sku=canonical))
        batch = _batch(db)
        # Two shipments to the same creator — one under alias, one under canonical.
        record_sample_shipment(db, sku=legacy, quantity=2,
                               shipped_at=datetime(2026, 5, 1), brand="smashbox",
                               import_batch_id=batch.id, creator_handle="@joe")
        record_sample_shipment(db, sku=canonical, quantity=1,
                               shipped_at=datetime(2026, 5, 2), brand="smashbox",
                               import_batch_id=batch.id, creator_handle="@joe")
        db.commit()

    with SessionLocal() as db:
        view = compute_samples_by_creator_view(db)

    # Both shipments go to "@joe" as legacy rows (no normalized creator_id on the
    # shipment calls above — creator_handle only, no get_or_create path since
    # creator_handle triggers it... actually they do get creator_id set via
    # get_or_create_creator. So they'll be in Pass 1.
    assert view.total_samples_sent == 3
    assert len(view.rows) == 1
    row = view.rows[0]
    assert row.distinct_sku_count == 1   # alias collapses to one canonical


# ---------------------------------------------------------------------------
# 4. Creator grouping
# ---------------------------------------------------------------------------

def test_normalized_creator_row():
    """Shipment with creator_handle → Pass 1 row (creator_id set, is_legacy=False)."""
    with SessionLocal() as db:
        batch = _batch(db)
        record_sample_shipment(
            db, sku="SBX-001", quantity=5,
            shipped_at=datetime(2026, 5, 1), brand="smashbox",
            import_batch_id=batch.id, creator_handle="@alice",
        )
        db.commit()

    with SessionLocal() as db:
        view = compute_samples_by_creator_view(db)

    assert len(view.rows) == 1
    row = view.rows[0]
    assert row.is_legacy is False
    assert row.creator_handle == "@alice"
    assert row.creator_id is not None
    assert row.total_samples_sent == 5


def test_legacy_creator_row():
    """Sample with creator_handle but no creator_id appears in Pass 2 (is_legacy=True)."""
    from app.models.sample import Sample

    with SessionLocal() as db:
        batch = _batch(db)
        # Insert Sample directly with creator_id=None to simulate legacy import.
        db.add(Sample(
            import_batch_id=batch.id,
            sku="SBX-002",
            quantity=3,
            shipped_at=datetime(2026, 5, 1),
            creator_handle="@legacy-bob",
            creator_id=None,
        ))
        db.commit()

    with SessionLocal() as db:
        view = compute_samples_by_creator_view(db)

    assert len(view.rows) == 1
    row = view.rows[0]
    assert row.is_legacy is True
    assert row.creator_handle == "@legacy-bob"
    assert row.creator_id is None


def test_one_creator_both_normalized_and_legacy_shows_two_rows():
    """One real person with a normalized Sample AND a legacy Sample → two rows.

    This is the expected behavior during the transition period before legacy
    Sample rows are backfilled with creator_id.
    The normalized row has is_legacy=False; the legacy row has is_legacy=True.
    """
    from app.models.sample import Sample

    with SessionLocal() as db:
        batch = _batch(db)
        # Normalized shipment — triggers get_or_create_creator, sets creator_id.
        record_sample_shipment(
            db, sku="SBX-001", quantity=4,
            shipped_at=datetime(2026, 5, 1), brand="smashbox",
            import_batch_id=batch.id, creator_handle="@carol",
        )
        # Legacy sample — same handle, creator_id=None (simulates old import).
        db.add(Sample(
            import_batch_id=batch.id,
            sku="SBX-001",
            quantity=2,
            shipped_at=datetime(2026, 4, 1),
            creator_handle="@carol",
            creator_id=None,
        ))
        db.commit()

    with SessionLocal() as db:
        view = compute_samples_by_creator_view(db)

    assert len(view.rows) == 2, "one creator, two rows — normalized + legacy"
    handles = {r.creator_handle for r in view.rows}
    assert "@carol" in handles
    legacy_rows = [r for r in view.rows if r.is_legacy]
    normalized_rows = [r for r in view.rows if not r.is_legacy]
    assert len(legacy_rows) == 1
    assert len(normalized_rows) == 1
    assert legacy_rows[0].creator_id is None
    assert normalized_rows[0].creator_id is not None


# ---------------------------------------------------------------------------
# 5. Cost suppression
# ---------------------------------------------------------------------------

def test_any_shipping_cost_false_when_all_null():
    """When no shipments have shipping_cost set, any_shipping_cost is False."""
    with SessionLocal() as db:
        batch = _batch(db)
        record_sample_shipment(
            db, sku="SBX-001", quantity=2,
            shipped_at=datetime(2026, 5, 1), brand="smashbox",
            import_batch_id=batch.id, creator_handle="@dave",
            shipping_cost=None,
        )
        db.commit()

    with SessionLocal() as db:
        view = compute_samples_by_creator_view(db)

    assert view.any_shipping_cost is False


def test_any_shipping_cost_true_when_any_non_null():
    """When at least one shipment has shipping_cost, any_shipping_cost is True."""
    with SessionLocal() as db:
        batch = _batch(db)
        record_sample_shipment(
            db, sku="SBX-001", quantity=2,
            shipped_at=datetime(2026, 5, 1), brand="smashbox",
            import_batch_id=batch.id, creator_handle="@eve",
            shipping_cost=Decimal("9.99"),
        )
        db.commit()

    with SessionLocal() as db:
        view = compute_samples_by_creator_view(db)

    assert view.any_shipping_cost is True
    assert view.rows[0].total_shipping_cost == Decimal("9.99")
