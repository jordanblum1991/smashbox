"""Stockout history + lost-sales estimate from the inventory snapshots."""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.import_batch import _utc_now_naive
from app.models.inventory_snapshot import InventorySnapshot
from app.models.sku import Sku
from app.services.demand.stockouts import (
    StockoutStat,
    compute_stockout_stats,
    estimate_lost_units,
)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _snap(db, batch_id, sku, on_hand, days_ago):
    db.add(InventorySnapshot(
        import_batch_id=batch_id, sku=sku, on_hand=on_hand,
        captured_at=_utc_now_naive() - timedelta(days=days_ago),
    ))


def test_counts_zero_readings_and_currently_out():
    with SessionLocal() as db:
        b = ImportBatch(kind=ImportFileKind.INVENTORY_SNAPSHOT,
                        status=ImportBatchStatus.COMPLETED, original_filename="s", stored_path="s")
        db.add(b); db.flush()
        _snap(db, b.id, "SBX-A", 5, 10)   # in stock
        _snap(db, b.id, "SBX-A", 0, 5)    # out
        _snap(db, b.id, "SBX-A", 0, 0)    # still out (latest)
        db.commit()
        out = compute_stockout_stats(db, window_days=30)
    s = out["SBX-A"]
    assert s.stockout_readings == 2
    assert s.total_readings == 3
    assert s.currently_out is True


def test_sku_never_out_is_absent():
    with SessionLocal() as db:
        b = ImportBatch(kind=ImportFileKind.INVENTORY_SNAPSHOT,
                        status=ImportBatchStatus.COMPLETED, original_filename="s", stored_path="s")
        db.add(b); db.flush()
        _snap(db, b.id, "SBX-OK", 10, 5)
        _snap(db, b.id, "SBX-OK", 8, 0)
        db.commit()
        out = compute_stockout_stats(db, window_days=30)
    assert "SBX-OK" not in out


def test_lost_units_is_days_out_times_velocity_zero_without_demand():
    stat = StockoutStat("SBX-A", stockout_readings=3, total_readings=5,
                        currently_out=True, last_out_at=datetime(2026, 6, 16))
    assert estimate_lost_units(stat, Decimal("2.0")) == 6   # 3 × 2
    assert estimate_lost_units(stat, Decimal("0")) == 0     # no demand → no lost sale
    assert estimate_lost_units(None, Decimal("5")) == 0


def test_detail_view_surfaces_stockout_and_lost_units():
    from app.reports.demand_planning import compute_sku_detail_view
    with SessionLocal() as db:
        db.add(Sku(sku="SBX-A", tiktok_sku_id="SBX-A", name="A",
                   brand="smashbox", unit_cogs=Decimal("5")))
        b = ImportBatch(kind=ImportFileKind.INVENTORY_SNAPSHOT,
                        status=ImportBatchStatus.COMPLETED, original_filename="s", stored_path="s")
        db.add(b); db.flush()
        _snap(db, b.id, "SBX-A", 10, 8)
        _snap(db, b.id, "SBX-A", 0, 0)
        db.commit()
        view = compute_sku_detail_view(db, "SBX-A")
    assert view is not None
    assert view.stockout is not None
    assert view.stockout.currently_out is True
    assert view.stockout.stockout_readings == 1
