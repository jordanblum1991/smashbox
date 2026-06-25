"""Shade/size family rollup on the inventory report.

Products that share a base name but differ by shade/size (each its own SBX code
+ own on-hand) collapse into ONE expandable parent group; single products,
bundles, and unmapped keys stay flat. Parent rows aggregate their members.
Status badge + days-of-cover come from the demand planner (degrade to neutral
when there's no sales signal, as in these fixtures)."""
from datetime import datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models.import_batch import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.inventory_snapshot import InventorySnapshot
from app.models.sku import Sku
from app.reports.inventory_report import (
    InventoryGroup,
    _badge_for,
    _family_key,
    _worst_badge,
    compute_inventory_report,
)
from app.services.demand.replenishment import ReplenishmentStatus


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _batch(db):
    b = ImportBatch(kind=ImportFileKind.INVENTORY_SNAPSHOT,
                    status=ImportBatchStatus.COMPLETED,
                    original_filename="t", stored_path="t")
    db.add(b); db.flush()
    return b


def _snap(db, b, sku, on_hand, when=datetime(2026, 6, 23, 7, 0)):
    db.add(InventorySnapshot(import_batch_id=b.id, sku=sku,
                             on_hand=on_hand, captured_at=when))


# ---------------- family key derivation (pure) ----------------

def test_family_key_strips_trailing_shade():
    assert _family_key("Wonder Foundation - Shade 1") == "WONDER FOUNDATION"
    assert _family_key("Wonder Foundation - Shade 12W") == "WONDER FOUNDATION"


def test_family_key_strips_trailing_size_paren():
    assert _family_key("Photo Finish Primer (1.7 oz)") == "PHOTO FINISH PRIMER"
    assert _family_key("Photo Finish Primer (0.5 oz)") == "PHOTO FINISH PRIMER"


def test_family_key_does_not_split_internal_hyphens():
    # "ALL-IN-ONE" / "ANTI-REDNESS" have no spaces around the hyphen → untouched.
    assert _family_key("Halo All-In-One Tinted Moisturizer") == \
        "HALO ALL-IN-ONE TINTED MOISTURIZER"


def test_family_key_none_for_empty():
    assert _family_key(None) is None
    assert _family_key("") is None


# ---------------- badge mapping (pure) ----------------

def test_badge_for_maps_statuses():
    assert _badge_for(ReplenishmentStatus.OUT_OF_STOCK) == "out"
    assert _badge_for(ReplenishmentStatus.AT_RISK) == "low"
    assert _badge_for(ReplenishmentStatus.REORDER_NOW) == "low"
    assert _badge_for(ReplenishmentStatus.HEALTHY) == "healthy"
    assert _badge_for(ReplenishmentStatus.OVERSTOCKED) == "overstock"
    assert _badge_for(ReplenishmentStatus.NO_VELOCITY) == "none"
    assert _badge_for(None) == "none"


def test_worst_badge_picks_most_urgent():
    assert _worst_badge(["healthy", "out", "none"]) == "out"
    assert _worst_badge(["healthy", "overstock"]) == "healthy"
    assert _worst_badge([]) == "none"


# ---------------- rollup behavior (integration) ----------------

def test_multi_shade_family_rolls_up_into_one_group():
    with SessionLocal() as db:
        b = _batch(db)
        for i, (code, oh, cogs) in enumerate([
            ("SBX-F1", 10, "4.00"), ("SBX-F2", 20, "5.00"), ("SBX-F3", 0, "6.00")
        ]):
            db.add(Sku(sku=code, name=f"Wonder Foundation - Shade {i+1}",
                       brand="smashbox", tiktok_sku_id=f"20{i}",
                       unit_cogs=Decimal(cogs)))
            _snap(db, b, code, oh)
        db.commit()
        view = compute_inventory_report(db)

    fam = [g for g in view.groups if g.is_family]
    assert len(fam) == 1
    g = fam[0]
    assert g.member_count == 3
    assert len(g.members) == 3
    assert g.label == "Wonder Foundation"
    # Aggregates summed across shades.
    assert g.sellable_on_hand == 30
    assert g.sellable_value == Decimal("140.00")   # 10*4 + 20*5 + 0*6
    # Per-unit COGS differs across shades → not shown at parent.
    assert g.unit_cogs is None


def test_single_product_stays_flat():
    with SessionLocal() as db:
        b = _batch(db)
        db.add(Sku(sku="SBX-S", name="Solo Primer", brand="smashbox",
                   tiktok_sku_id="900", unit_cogs=Decimal("3.00")))
        _snap(db, b, "SBX-S", 5)
        db.commit()
        view = compute_inventory_report(db)

    g = next(g for g in view.groups if g.sku_code == "SBX-S")
    assert g.is_family is False
    assert g.member_count == 1
    assert g.sellable_on_hand == 5


def test_flat_rows_still_present_for_csv_email():
    # The flat per-member `rows` list is preserved (CSV / xlsx / email read it).
    with SessionLocal() as db:
        b = _batch(db)
        db.add(Sku(sku="SBX-F1", name="Wonder Foundation - Shade 1",
                   brand="smashbox", tiktok_sku_id="201", unit_cogs=Decimal("4.00")))
        db.add(Sku(sku="SBX-F2", name="Wonder Foundation - Shade 2",
                   brand="smashbox", tiktok_sku_id="202", unit_cogs=Decimal("5.00")))
        _snap(db, b, "SBX-F1", 10)
        _snap(db, b, "SBX-F2", 20)
        db.commit()
        view = compute_inventory_report(db)

    codes = {r.sku_code for r in view.rows}
    assert codes == {"SBX-F1", "SBX-F2"}
    # New per-row fields exist.
    r = next(r for r in view.rows if r.sku_code == "SBX-F1")
    assert hasattr(r, "status")
    assert hasattr(r, "days_of_cover")


def test_page_renders_family_group_with_expandable_members():
    from fastapi.testclient import TestClient
    from app.main import app
    with SessionLocal() as db:
        b = _batch(db)
        for i in range(3):
            code = f"SBX-G{i}"
            db.add(Sku(sku=code, name=f"Glow Tint - Shade {i}", brand="smashbox",
                       tiktok_sku_id=f"30{i}", unit_cogs=Decimal("4.00")))
            _snap(db, b, code, 10)
        db.add(Sku(sku="SBX-SOLO", name="Solo Primer", brand="smashbox",
                   tiktok_sku_id="400", unit_cogs=Decimal("3.00")))
        _snap(db, b, "SBX-SOLO", 7)
        db.commit()

    html = TestClient(app).get("/reports/inventory").text
    assert "3 shades" in html                    # parent count chip
    assert 'data-member data-parent="GLOW TINT"' in html  # collapsible members
    assert 'data-key="cover"' in html            # new column header
    assert "Solo Primer" in html                 # singleton still listed


def test_no_velocity_yields_neutral_status_and_no_cover():
    with SessionLocal() as db:
        b = _batch(db)
        db.add(Sku(sku="SBX-S", name="Solo Primer", brand="smashbox",
                   tiktok_sku_id="900", unit_cogs=Decimal("3.00")))
        _snap(db, b, "SBX-S", 5)
        db.commit()
        view = compute_inventory_report(db)

    g = next(g for g in view.groups if g.sku_code == "SBX-S")
    assert g.status == "none"
    assert g.days_of_cover is None
