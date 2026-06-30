"""Shade/size family rollup on the inventory report.

Shades of one product share an SBX code base and differ only by the trailing
2-digit shade number (e.g. SBX-C5JK01..C5JK22); each shade is its own physical
code with its own on-hand. They collapse into ONE expandable parent group keyed
on that code base; single products (no 2-digit shade suffix), bundles, and
unmapped keys stay flat. Parent rows aggregate their members. Status badge +
days-of-cover come from the demand planner (neutral when there's no sales
signal, as in these fixtures)."""
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
    _common_label,
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

def test_family_key_is_code_base_without_shade_digits():
    assert _family_key("SBX-C5JK01") == "SBX-C5JK"
    assert _family_key("SBX-C5JK22") == "SBX-C5JK"
    assert _family_key("SBX-C57L40") == "SBX-C57L"


def test_family_key_none_when_no_two_digit_shade_suffix():
    # No trailing shade number → can't be a shade range; renders flat.
    assert _family_key("SBX-SOLO") is None
    assert _family_key("SBX-PRIMER") is None
    assert _family_key(None) is None
    assert _family_key("") is None


def test_common_label_picks_shared_word_prefix():
    assert _common_label(["Wonder Foundation Fair", "Wonder Foundation Tan"]) == \
        "Wonder Foundation"
    # Trailing size paren is ignored when finding the shared prefix.
    assert _common_label(["Glow Tint Light (1 oz)", "Glow Tint Deep (1 oz)"]) == \
        "Glow Tint"


def test_common_label_trims_trailing_separator():
    # A shared " - " / "-" shade delimiter must not leave a dangling dash.
    assert _common_label(["Lipstick - Red", "Lipstick - Blue"]) == "Lipstick"
    assert _common_label(["Highlighter- Opal", "Highlighter- Rose"]) == "Highlighter"


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
        for code, oh, cogs, shade in [
            ("SBX-WF01", 10, "4.00", "Fair"),
            ("SBX-WF02", 20, "5.00", "Light"),
            ("SBX-WF03", 0, "6.00", "Tan"),
        ]:
            db.add(Sku(sku=code, name=f"Wonder Foundation {shade}",
                       brand="smashbox", tiktok_sku_id=f"2{code[-2:]}",
                       unit_cogs=Decimal(cogs)))
            _snap(db, b, code, oh)
        db.commit()
        view = compute_inventory_report(db)

    fam = [g for g in view.groups if g.is_family]
    assert len(fam) == 1
    g = fam[0]
    assert g.key == "SBX-WF"
    assert g.member_count == 3
    assert len(g.members) == 3
    assert g.label == "Wonder Foundation"
    # Aggregates summed across shades.
    assert g.sellable_on_hand == 30
    assert g.sellable_value == Decimal("140.00")   # 10*4 + 20*5 + 0*6
    # Per-unit COGS differs across shades → not shown at parent.
    assert g.unit_cogs is None


def test_unmapped_zero_stock_rows_are_hidden():
    # Catalog-gap noise: an unmapped SKU (not in catalog) with no stock is dropped.
    # Unmapped-with-stock and mapped-zero-stock both stay.
    with SessionLocal() as db:
        b = _batch(db)
        _snap(db, b, "SBX-GHOST", 0)        # unmapped + zero -> hidden
        _snap(db, b, "SBX-REALGHOST", 4)    # unmapped + has stock -> shown
        db.add(Sku(sku="SBX-MAP", name="Mapped Primer", brand="smashbox",
                   tiktok_sku_id="555", unit_cogs=Decimal("3.00")))
        _snap(db, b, "SBX-MAP", 0)          # mapped + zero -> still shown
        db.commit()
        view = compute_inventory_report(db)

    canon = {m.canonical_sku for g in view.groups for m in g.members}
    assert "SBX-GHOST" not in canon
    assert "SBX-REALGHOST" in canon
    assert any(g.sku_code == "SBX-MAP" for g in view.groups)


def test_family_override_groups_even_unrelated_codes():
    # Two SKUs with unrelated code bases but the same manual `family` group into
    # one family, labeled by the family value.
    with SessionLocal() as db:
        b = _batch(db)
        db.add(Sku(sku="SBX-C70N01", name="Cali Contour Palette- Medium To Dark",
                   brand="smashbox", tiktok_sku_id="701", unit_cogs=Decimal("5.00"),
                   family="Cali Contour Palette"))
        db.add(Sku(sku="SBX-C49T01", name="Cali Contour Palette -Light To Medium",
                   brand="smashbox", tiktok_sku_id="491", unit_cogs=Decimal("5.00"),
                   family="Cali Contour Palette"))
        _snap(db, b, "SBX-C70N01", 10)
        _snap(db, b, "SBX-C49T01", 5)
        db.commit()
        view = compute_inventory_report(db)

    fam = [g for g in view.groups if g.is_family and g.label == "Cali Contour Palette"]
    assert len(fam) == 1
    assert {m.sku_code for m in fam[0].members} == {"SBX-C70N01", "SBX-C49T01"}
    assert fam[0].member_count == 2


def test_sample_on_order_folds_into_row_and_total():
    from app.models.sample_inbound_order import SampleInboundOrder, SampleInboundOrderLine
    with SessionLocal() as db:
        b = _batch(db)
        db.add(Sku(sku="SBX-X", name="X", brand="smashbox", tiktok_sku_id="111",
                   unit_cogs=Decimal("3.00")))
        _snap(db, b, "SBX-X", 5)                       # on-hand row
        o = SampleInboundOrder(source="s", status="open"); db.add(o); db.flush()
        db.add(SampleInboundOrderLine(sample_inbound_order_id=o.id, sku="SBX-X", quantity=12))
        o2 = SampleInboundOrder(source="s", status="received"); db.add(o2); db.flush()
        db.add(SampleInboundOrderLine(sample_inbound_order_id=o2.id, sku="SBX-X", quantity=99))
        db.commit()
        view = compute_inventory_report(db)

    row = next(r for r in view.rows if r.sku_code == "SBX-X")
    assert row.sample_in_transit == 12                 # open inbound counts
    assert view.total_sample_in_transit == 12          # received (99) excluded


def test_single_product_stays_flat():
    with SessionLocal() as db:
        b = _batch(db)
        db.add(Sku(sku="SBX-SOLO", name="Solo Primer", brand="smashbox",
                   tiktok_sku_id="900", unit_cogs=Decimal("3.00")))
        _snap(db, b, "SBX-SOLO", 5)
        db.commit()
        view = compute_inventory_report(db)

    g = next(g for g in view.groups if g.sku_code == "SBX-SOLO")
    assert g.is_family is False
    assert g.member_count == 1
    assert g.sellable_on_hand == 5


def test_flat_rows_still_present_for_csv_email():
    # The flat per-member `rows` list is preserved (CSV / xlsx / email read it).
    with SessionLocal() as db:
        b = _batch(db)
        db.add(Sku(sku="SBX-WF01", name="Wonder Foundation Fair",
                   brand="smashbox", tiktok_sku_id="201", unit_cogs=Decimal("4.00")))
        db.add(Sku(sku="SBX-WF02", name="Wonder Foundation Light",
                   brand="smashbox", tiktok_sku_id="202", unit_cogs=Decimal("5.00")))
        _snap(db, b, "SBX-WF01", 10)
        _snap(db, b, "SBX-WF02", 20)
        db.commit()
        view = compute_inventory_report(db)

    codes = {r.sku_code for r in view.rows}
    assert codes == {"SBX-WF01", "SBX-WF02"}
    # New per-row fields exist.
    r = next(r for r in view.rows if r.sku_code == "SBX-WF01")
    assert hasattr(r, "status")
    assert hasattr(r, "days_of_cover")


def test_no_velocity_stocked_sku_shows_no_sales_label():
    # A SKU with stock but no recent sales has no computable status/cover, so the
    # cells read "No sales" (not a bare dash).
    from fastapi.testclient import TestClient
    from app.main import app
    with SessionLocal() as db:
        b = _batch(db)
        db.add(Sku(sku="SBX-SLOW", name="Slow Mover", brand="smashbox",
                   tiktok_sku_id="777", unit_cogs=Decimal("3.00")))
        _snap(db, b, "SBX-SLOW", 12)        # has stock, no orders -> no velocity
        db.commit()

    html = TestClient(app).get("/reports/inventory").text
    assert "No sales" in html


def test_single_product_row_renders_its_name_in_the_visible_cell():
    # Regression: a non-family (single) SKU must show its product name in the
    # Product Name column, not the "—" placeholder. The title-cased form only
    # appears if the visible cell rendered it (the data-name attr is raw-upper).
    from fastapi.testclient import TestClient
    from app.main import app
    with SessionLocal() as db:
        b = _batch(db)
        db.add(Sku(sku="SBX-SOLO", name="WIDGET PRIMER", brand="smashbox",
                   tiktok_sku_id="900", unit_cogs=Decimal("3.00")))
        _snap(db, b, "SBX-SOLO", 5)
        db.commit()

    html = TestClient(app).get("/reports/inventory").text
    assert "Widget Primer" in html


def test_page_renders_family_group_with_expandable_members():
    from fastapi.testclient import TestClient
    from app.main import app
    with SessionLocal() as db:
        b = _batch(db)
        for i, shade in enumerate(["Fair", "Light", "Tan"]):
            code = f"SBX-GL0{i}"
            db.add(Sku(sku=code, name=f"Glow Tint {shade}", brand="smashbox",
                       tiktok_sku_id=f"30{i}", unit_cogs=Decimal("4.00")))
            _snap(db, b, code, 10)
        db.add(Sku(sku="SBX-SOLO", name="Solo Primer", brand="smashbox",
                   tiktok_sku_id="400", unit_cogs=Decimal("3.00")))
        _snap(db, b, "SBX-SOLO", 7)
        db.commit()

    html = TestClient(app).get("/reports/inventory").text
    assert "3 shades" in html                       # parent count chip
    assert 'data-member data-parent="SBX-GL"' in html  # collapsible members
    assert 'data-key="cover"' in html               # new column header
    assert "Solo Primer" in html                    # singleton still listed


def _add_velocity(db, ob, ttid, units_per_day, days=30):
    """Seed shipped PAID orders so the demand planner computes velocity for `ttid`."""
    from datetime import timedelta
    from app.models.order import Order, OrderLine, OrderType
    base = datetime.now()
    for d in range(days):
        o = Order(import_batch_id=ob.id, tiktok_order_id=f"OV{ttid}-{d}",
                  order_type=OrderType.PAID, status="Shipped",
                  placed_at=base - timedelta(days=d), brand="smashbox")
        db.add(o); db.flush()
        db.add(OrderLine(order_id=o.id, sku=ttid, quantity=units_per_day))


def test_family_status_is_worst_plus_affected_count():
    with SessionLocal() as db:
        b = _batch(db)
        ob = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS,
                         status=ImportBatchStatus.COMPLETED,
                         original_filename="o", stored_path="o")
        db.add(ob); db.flush()
        # Golden shade: 0 on-hand but still selling -> Out. Ivory: deep stock -> not out.
        db.add(Sku(sku="SBX-MX01", name="Mixy Foundation Golden", brand="smashbox",
                   tiktok_sku_id="501", unit_cogs=Decimal("4.00")))
        db.add(Sku(sku="SBX-MX02", name="Mixy Foundation Ivory", brand="smashbox",
                   tiktok_sku_id="502", unit_cogs=Decimal("4.00")))
        _snap(db, b, "SBX-MX01", 0)
        _snap(db, b, "SBX-MX02", 800)
        _add_velocity(db, ob, "501", 3)
        _add_velocity(db, ob, "502", 1)
        db.commit()
        view = compute_inventory_report(db)

    g = next(g for g in view.groups if g.key == "SBX-MX")
    assert g.is_family
    assert g.status == "out"          # worst shade (Golden at 0) wins
    assert g.status_count == 1        # but only 1 of 2 shades is out — not the whole family


def test_no_velocity_yields_neutral_status_and_no_cover():
    with SessionLocal() as db:
        b = _batch(db)
        db.add(Sku(sku="SBX-SOLO", name="Solo Primer", brand="smashbox",
                   tiktok_sku_id="900", unit_cogs=Decimal("3.00")))
        _snap(db, b, "SBX-SOLO", 5)
        db.commit()
        view = compute_inventory_report(db)

    g = next(g for g in view.groups if g.sku_code == "SBX-SOLO")
    assert g.status == "none"
    assert g.days_of_cover is None
