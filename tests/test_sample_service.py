"""Tests for app/services/sample_service.py.

Covers five invariants:
  1. record_sample_shipment creates both a Sample row and a ledger OUT,
     linked by sample_id, with matching quantity.
  2. The ledger OUT decrements get_sample_on_hand correctly.
  3. SYNC INVARIANT: if the ledger write fails mid-transaction, the Sample row
     is also rolled back — neither row persists. This is the critical test.
  4. record_sample_receipt adds to on-hand; a receipt then a shipment nets.
  5. Aliased SKUs consolidate: a receipt under a legacy code and a shipment
     under the canonical code net against the same balance.
"""
from datetime import datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.creator import Creator
from app.models.sample import Sample
from app.models.sample_inventory_movement import SampleInventoryMovement, SampleMovementType
from app.models.sku_alias import SkuAlias
from app.services.sample_service import (
    get_or_create_creator,
    get_sample_on_hand,
    record_sample_receipt,
    record_sample_shipment,
)


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
# 1. Both rows created, linked by sample_id
# ---------------------------------------------------------------------------

def test_shipment_creates_both_rows_linked():
    with SessionLocal() as db:
        batch = _batch(db)
        sample, movement = record_sample_shipment(
            db,
            sku="SBX-001",
            quantity=3,
            shipped_at=datetime(2026, 5, 1),
            brand="smashbox",
            import_batch_id=batch.id,
            creator_handle="@creator1",
        )
        db.commit()

    with SessionLocal() as db:
        s = db.query(Sample).one()
        m = db.query(SampleInventoryMovement).one()

        assert s.sku == "SBX-001"
        assert s.quantity == 3
        assert s.creator_handle == "@creator1"

        assert m.movement_type == SampleMovementType.OUT
        assert m.quantity == 3
        assert m.sku == "SBX-001"
        assert m.sample_id == s.id   # FK link enforced


# ---------------------------------------------------------------------------
# 2. OUT decrements get_sample_on_hand
# ---------------------------------------------------------------------------

def test_shipment_decrements_on_hand():
    with SessionLocal() as db:
        batch = _batch(db)
        record_sample_receipt(
            db, sku="SBX-001", quantity=10,
            received_at=datetime(2026, 5, 1), brand="smashbox",
        )
        record_sample_shipment(
            db, sku="SBX-001", quantity=3,
            shipped_at=datetime(2026, 5, 2), brand="smashbox",
            import_batch_id=batch.id,
        )
        db.commit()

    with SessionLocal() as db:
        on_hand = get_sample_on_hand(db)
        assert on_hand.get("SBX-001") == 7  # 10 − 3


# ---------------------------------------------------------------------------
# 3. SYNC INVARIANT: ledger failure rolls back the Sample row too
# ---------------------------------------------------------------------------

def test_sync_invariant_rollback_on_ledger_failure(monkeypatch):
    """If SampleInventoryMovement creation raises, the Sample flush is also
    rolled back. Neither row should survive in the DB."""
    # Commit the batch in its own session so it survives the rollback below.
    with SessionLocal() as db:
        batch = _batch(db)
        db.commit()
        batch_id = batch.id  # capture inside session — object is detached after close

    def ledger_bomb(self, **kwargs):
        raise RuntimeError("forced ledger failure")

    monkeypatch.setattr(SampleInventoryMovement, "__init__", ledger_bomb)

    # The RuntimeError propagates out of the SessionLocal block, which causes
    # Session.__exit__ to call close() → rollback(). The flushed Sample row
    # is undone. Nothing is committed.
    with pytest.raises(RuntimeError, match="forced ledger failure"):
        with SessionLocal() as db:
            record_sample_shipment(
                db,
                sku="SBX-001",
                quantity=2,
                shipped_at=datetime(2026, 5, 1),
                brand="smashbox",
                import_batch_id=batch_id,
            )
            db.commit()  # never reached

    with SessionLocal() as db:
        assert db.query(Sample).count() == 0, "Sample must not persist after ledger failure"
        assert db.query(SampleInventoryMovement).count() == 0, "Movement must not persist"


# ---------------------------------------------------------------------------
# 4. Receipt adds to on-hand; receipt + shipment nets correctly
# ---------------------------------------------------------------------------

def test_receipt_adds_on_hand():
    with SessionLocal() as db:
        record_sample_receipt(
            db, sku="SBX-001", quantity=5,
            received_at=datetime(2026, 5, 1), brand="smashbox",
        )
        db.commit()

    with SessionLocal() as db:
        on_hand = get_sample_on_hand(db)
        assert on_hand.get("SBX-001") == 5


def test_receipt_then_shipment_nets():
    with SessionLocal() as db:
        batch = _batch(db)
        record_sample_receipt(
            db, sku="SBX-001", quantity=10,
            received_at=datetime(2026, 5, 1), brand="smashbox",
        )
        record_sample_shipment(
            db, sku="SBX-001", quantity=4,
            shipped_at=datetime(2026, 5, 2), brand="smashbox",
            import_batch_id=batch.id,
        )
        db.commit()

    with SessionLocal() as db:
        on_hand = get_sample_on_hand(db)
        assert on_hand.get("SBX-001") == 6   # 10 − 4


def test_zero_balance_omitted():
    """SKUs at net zero should not appear in get_sample_on_hand."""
    with SessionLocal() as db:
        batch = _batch(db)
        record_sample_receipt(
            db, sku="SBX-001", quantity=5,
            received_at=datetime(2026, 5, 1), brand="smashbox",
        )
        record_sample_shipment(
            db, sku="SBX-001", quantity=5,
            shipped_at=datetime(2026, 5, 2), brand="smashbox",
            import_batch_id=batch.id,
        )
        db.commit()

    with SessionLocal() as db:
        on_hand = get_sample_on_hand(db)
        assert "SBX-001" not in on_hand


# ---------------------------------------------------------------------------
# 5. Aliased SKUs consolidate into one balance
# ---------------------------------------------------------------------------

def test_aliased_skus_consolidate():
    """Receipt written under a legacy code before alias registration, then a
    shipment under the canonical code — get_sample_on_hand must net them."""
    legacy = "C001"
    canonical = "SBX-C001"

    # Write the receipt with the legacy code, no alias registered yet.
    # Pass alias_map={} explicitly to simulate pre-alias state.
    with SessionLocal() as db:
        record_sample_receipt(
            db, sku=legacy, quantity=8,
            received_at=datetime(2026, 5, 1), brand="smashbox",
            alias_map={},  # no alias registered at receipt time
        )
        db.commit()

    # Register alias: legacy → canonical.
    with SessionLocal() as db:
        db.add(SkuAlias(alias_sku=legacy, canonical_sku=canonical))
        db.commit()

    # Shipment under canonical — alias_map will rewrite it (no-op, already canonical).
    with SessionLocal() as db:
        batch = _batch(db)
        record_sample_shipment(
            db, sku=canonical, quantity=3,
            shipped_at=datetime(2026, 5, 2), brand="smashbox",
            import_batch_id=batch.id,
        )
        db.commit()

    # get_sample_on_hand applies alias_map: legacy code in the first ledger row
    # collapses to canonical, so both rows net against the same key.
    with SessionLocal() as db:
        on_hand = get_sample_on_hand(db)
        assert canonical in on_hand, "canonical SKU must appear in balance"
        assert legacy not in on_hand, "legacy code must be collapsed by alias map"
        assert on_hand[canonical] == 5   # 8 − 3


# ---------------------------------------------------------------------------
# get_or_create_creator — idempotency and auto-link
# ---------------------------------------------------------------------------

def test_get_or_create_creator_idempotent():
    with SessionLocal() as db:
        c1 = get_or_create_creator(db, handle="@joe", brand="smashbox")
        db.flush()
        c2 = get_or_create_creator(db, handle="@joe", brand="smashbox")
        db.commit()
        assert c1.id == c2.id
        assert db.query(Creator).count() == 1


def test_shipment_auto_links_creator():
    """When creator_handle is given, record_sample_shipment creates a Creator
    row and populates Sample.creator_id."""
    with SessionLocal() as db:
        batch = _batch(db)
        sample, _ = record_sample_shipment(
            db,
            sku="SBX-001",
            quantity=1,
            shipped_at=datetime(2026, 5, 1),
            brand="smashbox",
            import_batch_id=batch.id,
            creator_handle="@influencer",
        )
        db.commit()

    with SessionLocal() as db:
        s = db.query(Sample).one()
        assert s.creator_id is not None
        creator = db.get(Creator, s.creator_id)
        assert creator.handle == "@influencer"
        assert creator.platform == "unknown"  # ORM default applied
