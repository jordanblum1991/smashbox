"""Tests for /admin/skus add-SKU route.

Covers:
  1. Happy path — create with full field set.
  2. Required-field validation — missing name, missing sku.
  3. Uniqueness (Option A):
       a. Duplicate tiktok_sku_id rejected.
       b. (sku, tiktok_sku_id IS NULL) duplicate rejected.
       c. Same SBX code with different tiktok_sku_ids → ALLOWED (variations).
       d. SBX code that already exists with a tiktok_sku_id, plus a new row
          for the same code WITHOUT a tiktok_sku_id → ALLOWED.
  4. STRICT numeric rejection (LOAD-BEARING — unit_cogs feeds P&L/reorder):
       garbage MSRP / unit_cogs / lead_time / safety_pct → error redirect;
       nothing inserted.
  5. Range validation:
       a. Negative unit_cogs rejected.
       b. safety_stock_pct out of [0, 100] rejected.
       c. service_level not in SERVICE_LEVEL_Z_TABLE rejected.
  6. Blank optionals → procurement fields stored as None (NOT 0/1);
     blank msrp/unit_cogs → 0.
  7. Checkbox + status handling — is_reorderable absent → False;
     active_status Inactive → is_active False.
"""
from datetime import datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models.import_batch import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.order import Order, OrderLine
from app.models.sku import Sku
from app.routers.admin import create_sku


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _call(**kwargs):
    """Invoke create_sku directly with valid defaults; override per test."""
    defaults = dict(
        name="Test Product",
        sku="SBX-TEST-001",
        tiktok_alt_sku=None,
        tiktok_sku_id=None,
        brand="TestBrand",
        category=None,
        item_type=None,
        family=None,
        msrp="50.00",
        unit_cogs="10.0000",
        active_status="Active",
        lead_time_days="",
        moq="",
        case_pack="",
        safety_stock_pct="",
        service_level="",
        is_reorderable="on",
    )
    defaults.update(kwargs)
    with SessionLocal() as db:
        return create_sku(db=db, **defaults)


# 1. Happy path
def test_create_sku_with_full_field_set():
    resp = _call(
        tiktok_sku_id="9000000000000000001",
        tiktok_alt_sku="C00099",
        brand="Smashbox",
        category="Eye",
        item_type="Mascara",
        lead_time_days="45",
        moq="500",
        case_pack="12",
        safety_stock_pct="15.5",
        service_level="0.95",
    )
    assert resp.status_code == 303
    # Add-SKU now redirects to the consolidated Catalog page's SKUs tab.
    assert resp.headers["location"].startswith("/admin/catalog?tab=skus")
    assert "notice=" in resp.headers["location"]
    with SessionLocal() as db:
        s = db.query(Sku).one()
        assert s.name == "Test Product"
        assert s.sku == "SBX-TEST-001"
        assert s.tiktok_sku_id == "9000000000000000001"
        assert s.tiktok_alt_sku == "C00099"
        assert s.unit_cogs == Decimal("10.0000")
        assert s.lead_time_days == 45
        assert s.moq == 500
        assert s.case_pack == 12
        assert s.safety_stock_pct == Decimal("15.50")
        assert s.service_level == Decimal("0.95")
        assert s.is_reorderable is True
        assert s.is_active is True


# 2. Required-field validation
def test_missing_name_is_rejected():
    resp = _call(name="")
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    with SessionLocal() as db:
        assert db.query(Sku).count() == 0


def test_missing_sku_code_is_rejected():
    resp = _call(sku="")
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    with SessionLocal() as db:
        assert db.query(Sku).count() == 0


# 3a. Duplicate tiktok_sku_id rejected
def test_duplicate_tiktok_sku_id_is_rejected():
    _call(tiktok_sku_id="9000000000000000001")
    resp = _call(name="Other", sku="SBX-OTHER-001", tiktok_sku_id="9000000000000000001")
    assert resp.status_code == 303
    assert "already exists" in resp.headers["location"].lower().replace("+", " ").replace("%20", " ")
    with SessionLocal() as db:
        assert db.query(Sku).count() == 1


# 3b. (sku, tiktok_sku_id IS NULL) duplicate rejected
def test_same_sku_both_without_tiktok_id_is_rejected():
    """Two rows with the same SBX code AND no tiktok_sku_id would be
    indistinguishable — block it."""
    _call(sku="SBX-DUP", tiktok_sku_id=None)
    resp = _call(name="Second", sku="SBX-DUP", tiktok_sku_id=None)
    assert resp.status_code == 303
    assert "already exists" in resp.headers["location"].lower().replace("+", " ").replace("%20", " ")
    with SessionLocal() as db:
        assert db.query(Sku).count() == 1


# 3c. Same SBX code, different tiktok_sku_ids → ALLOWED (variation case)
def test_same_sbx_code_with_different_tiktok_ids_is_allowed():
    _call(sku="SBX-VARIES", tiktok_sku_id="9000000000000000010")
    resp = _call(name="Variation 2", sku="SBX-VARIES", tiktok_sku_id="9000000000000000011")
    assert resp.status_code == 303
    assert "notice=" in resp.headers["location"]
    with SessionLocal() as db:
        assert db.query(Sku).filter_by(sku="SBX-VARIES").count() == 2


# 3d. SBX code with tiktok_sku_id, then same SBX code without tiktok_sku_id
def test_existing_with_tiktok_id_does_not_block_new_without_tiktok_id():
    _call(sku="SBX-MIXED", tiktok_sku_id="9000000000000000020")
    resp = _call(name="Unlisted", sku="SBX-MIXED", tiktok_sku_id=None)
    assert resp.status_code == 303
    assert "notice=" in resp.headers["location"]
    with SessionLocal() as db:
        assert db.query(Sku).filter_by(sku="SBX-MIXED").count() == 2


# 4. STRICT numeric rejection — load-bearing
def test_garbage_unit_cogs_is_rejected_not_coerced():
    """unit_cogs feeds reorder math + every P&L COGS calc; silent coerce to 0
    would invisibly inflate margins on every order using this SKU."""
    resp = _call(unit_cogs="not-a-number")
    assert resp.status_code == 303
    assert "must" in resp.headers["location"].lower()
    with SessionLocal() as db:
        assert db.query(Sku).count() == 0


def test_garbage_msrp_is_rejected_not_coerced():
    resp = _call(msrp="totally-broken")
    assert resp.status_code == 303
    assert "must" in resp.headers["location"].lower()
    with SessionLocal() as db:
        assert db.query(Sku).count() == 0


def test_garbage_lead_time_days_is_rejected_not_coerced():
    resp = _call(lead_time_days="forty-five")
    assert resp.status_code == 303
    assert "whole" in resp.headers["location"].lower()
    with SessionLocal() as db:
        assert db.query(Sku).count() == 0


def test_garbage_safety_stock_pct_is_rejected():
    resp = _call(safety_stock_pct="fifteen")
    assert resp.status_code == 303
    assert "must" in resp.headers["location"].lower()
    with SessionLocal() as db:
        assert db.query(Sku).count() == 0


# 5. Range validation
def test_negative_unit_cogs_is_rejected():
    resp = _call(unit_cogs="-5")
    assert resp.status_code == 303
    assert "at least 0" in resp.headers["location"].lower().replace("+", " ").replace("%20", " ")
    with SessionLocal() as db:
        assert db.query(Sku).count() == 0


def test_safety_stock_pct_above_100_is_rejected():
    resp = _call(safety_stock_pct="120")
    assert resp.status_code == 303
    assert "at most 100" in resp.headers["location"].lower().replace("+", " ").replace("%20", " ")
    with SessionLocal() as db:
        assert db.query(Sku).count() == 0


def test_safety_stock_pct_below_0_is_rejected():
    resp = _call(safety_stock_pct="-1")
    assert resp.status_code == 303
    assert "at least 0" in resp.headers["location"].lower().replace("+", " ").replace("%20", " ")
    with SessionLocal() as db:
        assert db.query(Sku).count() == 0


def test_service_level_not_in_table_is_rejected():
    """SERVICE_LEVEL_Z_TABLE has discrete values (0.90, 0.95, 0.975). Any
    other value is rejected — the dropdown only offers valid choices, so this
    catches browser-bypass / curl-bypass attempts."""
    resp = _call(service_level="0.5")
    assert resp.status_code == 303
    assert "service level must be one of" in resp.headers["location"].lower().replace("+", " ").replace("%20", " ")
    with SessionLocal() as db:
        assert db.query(Sku).count() == 0


# 6. Blank optionals → None for procurement; 0 for msrp/unit_cogs
def test_blank_procurement_fields_stored_as_none():
    """Blank procurement fields mean 'use the planner's global default' —
    NOT zero. Critical distinction: lead_time_days=0 means 'goods arrive
    instantly'; lead_time_days=None means 'planner falls back to default'."""
    _call(lead_time_days="", moq="", case_pack="", safety_stock_pct="",
          service_level="", msrp="", unit_cogs="")
    with SessionLocal() as db:
        s = db.query(Sku).one()
        assert s.lead_time_days is None
        assert s.moq is None
        assert s.case_pack is None
        assert s.safety_stock_pct is None
        assert s.service_level is None
        # msrp/unit_cogs default to 0 (model default), not None
        assert s.msrp == Decimal("0")
        assert s.unit_cogs == Decimal("0")


# 7. Checkbox + status handling
def test_reorderable_checkbox_absent_stores_false():
    """HTML checkboxes: present → 'on'; absent → no value at all. The route
    converts that to bool: present → True, absent → False."""
    _call(is_reorderable=None)
    with SessionLocal() as db:
        assert db.query(Sku).one().is_reorderable is False


def test_active_status_inactive_stores_false():
    _call(active_status="Inactive")
    with SessionLocal() as db:
        assert db.query(Sku).one().is_active is False


# ---------------------------------------------------------------------------
# 8. Paste-artifact regression: trailing commas/semicolons on identifier
# fields must NOT bypass the uniqueness check. Triggered by live test-drive
# where a user pasted SKUs from a CSV and accidentally got three "duplicate"
# rows because each had a different trailing punctuation pattern.
# ---------------------------------------------------------------------------

def test_trailing_comma_on_sku_normalizes_and_collides():
    """A SKU code pasted with a trailing comma must be treated as the same
    value as the clean version — the uniqueness check must catch the dup."""
    _call(sku="SBX-DUP-CSV", tiktok_sku_id=None)
    resp = _call(name="Dirty paste", sku="SBX-DUP-CSV,", tiktok_sku_id=None)
    assert resp.status_code == 303
    assert "already exists" in resp.headers["location"].lower().replace("+", " ").replace("%20", " ")
    with SessionLocal() as db:
        assert db.query(Sku).filter_by(sku="SBX-DUP-CSV").count() == 1
        assert db.query(Sku).filter_by(sku="SBX-DUP-CSV,").count() == 0  # never stored


def test_trailing_comma_on_tiktok_sku_id_normalizes_and_collides():
    _call(tiktok_sku_id="9000000000000000300")
    resp = _call(
        name="Dirty paste",
        sku="SBX-DIFF",
        tiktok_sku_id="9000000000000000300,",
    )
    assert resp.status_code == 303
    assert "already exists" in resp.headers["location"].lower().replace("+", " ").replace("%20", " ")
    with SessionLocal() as db:
        assert db.query(Sku).filter_by(tiktok_sku_id="9000000000000000300").count() == 1
        assert db.query(Sku).filter_by(tiktok_sku_id="9000000000000000300,").count() == 0


def test_tiktok_sku_id_with_letters_is_rejected():
    """TikTok IDs are numeric. Letters in the field = paste junk or typo."""
    resp = _call(tiktok_sku_id="9000abc")
    assert resp.status_code == 303
    assert "must contain only digits" in resp.headers["location"].lower().replace("+", " ").replace("%20", " ")
    with SessionLocal() as db:
        assert db.query(Sku).count() == 0


def test_tiktok_sku_id_with_internal_comma_is_rejected():
    """Embedded commas (not just trailing) don't get stripped — they trip the
    digits-only check instead, surfacing a clearer error to the user."""
    resp = _call(tiktok_sku_id="9000,000")
    assert resp.status_code == 303
    assert "must contain only digits" in resp.headers["location"].lower().replace("+", " ").replace("%20", " ")
    with SessionLocal() as db:
        assert db.query(Sku).count() == 0


def test_clean_tiktok_sku_id_still_works():
    """Sanity check that the digits-only validation doesn't reject a valid
    long numeric ID (real TikTok IDs are 19 digits)."""
    resp = _call(tiktok_sku_id="1729482705198552235")  # real-shape 19-digit ID
    assert resp.status_code == 303
    assert "notice=" in resp.headers["location"]
    with SessionLocal() as db:
        assert db.query(Sku).count() == 1


# ---------------------------------------------------------------------------
# 9. Web-create retroactively backfills unmapped OrderLines. Mirrors the XLSX
# importer behavior — adding a SKU via the web admin should resolve any
# already-loaded lines that referenced it before the master row existed.
# ---------------------------------------------------------------------------
def test_create_sku_backfills_unmapped_order_line_cogs():
    tiktok_id = "9000000000000005555"
    with SessionLocal() as db:
        batch = ImportBatch(
            kind=ImportFileKind.TIKTOK_ORDERS,
            status=ImportBatchStatus.COMPLETED,
            original_filename="seed.csv",
            stored_path="/tmp/seed.csv",
        )
        db.add(batch)
        db.flush()
        order = Order(
            import_batch_id=batch.id,
            tiktok_order_id="TT-SKU-BACKFILL-1",
            placed_at=datetime(2026, 5, 1, 12, 0, 0),
            status="Completed",
            brand="Smashbox",
        )
        db.add(order)
        db.flush()
        db.add(OrderLine(
            order_id=order.id,
            sku=tiktok_id,
            quantity=1,
            unit_cogs_snapshot=Decimal("0"),
        ))
        db.commit()

    resp = _call(name="Backfill Target", sku="SBX-BACKFILL", tiktok_sku_id=tiktok_id,
                 unit_cogs="7.25")
    assert resp.status_code == 303
    assert "notice=" in resp.headers["location"]

    with SessionLocal() as db:
        line = db.query(OrderLine).filter_by(sku=tiktok_id).one()
        assert line.unit_cogs_snapshot == Decimal("7.2500")
