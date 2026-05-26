"""Tests for /admin/bundles add-bundle route.

Covers:
  1. Happy path — create with N components, all fields populated.
  2. Bundle SKU synthesis — bundle_sku = first_component_sku + "-BUNDLE",
     matching importer behavior.
  3. Required-field validation — missing name, missing tiktok_sku_id.
  4. Uniqueness — duplicate TikTok SKU ID rejected with error redirect.
  5. STRICT numeric rejection (LOAD-BEARING) — garbage MSRP / COGS / qty
     values get an error redirect, NOT silent coercion to 0. Silent coercion
     would overstate P&L margins on every order containing the bundle.
  6. Blank numerics OK — blank money → 0 ('not specified'); blank qty → 1.
  7. At least one non-blank component required.
"""
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models.bundle import Bundle, BundleComponent
from app.routers.admin import create_bundle


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _call(**kwargs):
    """Invoke create_bundle directly with valid defaults; override per test."""
    defaults = dict(
        name="Test Bundle",
        variation=None,
        tiktok_sku_id="9000000000000000099",
        brand="TestBrand",
        active_status="Active",
        msrp="100.00",
        selling_price="80.00",
        component_sku=["SBX-TEST-001"],
        component_name=["Test Component"],
        component_qty=["1"],
        component_msrp=["50.00"],
        component_cogs=["10.00"],
    )
    defaults.update(kwargs)
    with SessionLocal() as db:
        return create_bundle(db=db, **defaults)


# 1. Happy path
def test_create_bundle_with_two_components():
    resp = _call(
        component_sku=["SBX-A", "SBX-B"],
        component_name=["A item", "B item"],
        component_qty=["1", "2"],
        component_msrp=["25", "15"],
        component_cogs=["5", "3"],
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/admin/bundles?notice=")
    with SessionLocal() as db:
        b = db.query(Bundle).one()
        assert b.name == "Test Bundle"
        assert b.tiktok_sku_id == "9000000000000000099"
        assert {c.component_sku for c in b.components} == {"SBX-A", "SBX-B"}


# 2. bundle_sku synthesis
def test_create_bundle_synthesizes_bundle_sku_from_first_component():
    _call(component_sku=["SBX-PARENT"], component_name=[""], component_qty=["1"],
          component_msrp=["0"], component_cogs=["0"])
    with SessionLocal() as db:
        assert db.query(Bundle).one().bundle_sku == "SBX-PARENT-BUNDLE"


# 3. Required-field validation
def test_missing_name_is_rejected():
    resp = _call(name="")
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    with SessionLocal() as db:
        assert db.query(Bundle).count() == 0


def test_missing_tiktok_sku_id_is_rejected():
    resp = _call(tiktok_sku_id="")
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    with SessionLocal() as db:
        assert db.query(Bundle).count() == 0


# 4. Uniqueness
def test_duplicate_tiktok_sku_id_is_rejected():
    _call()
    resp = _call(name="Another Bundle")  # same tiktok_sku_id default
    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert "already" in loc.lower()
    with SessionLocal() as db:
        assert db.query(Bundle).count() == 1


# 5. STRICT numeric rejection — load-bearing
def test_garbage_msrp_is_rejected_not_coerced():
    """Silent coercion to 0 would invisibly understate the bundle's value on
    every order. Reject garbage; require user to fix it."""
    resp = _call(msrp="not-a-number")
    assert resp.status_code == 303
    assert "must" in resp.headers["location"].lower()
    with SessionLocal() as db:
        assert db.query(Bundle).count() == 0, "no bundle on validation failure"


def test_garbage_component_cogs_is_rejected_not_coerced():
    resp = _call(component_cogs=["broken-value"])
    assert resp.status_code == 303
    assert "must" in resp.headers["location"].lower()
    with SessionLocal() as db:
        assert db.query(Bundle).count() == 0
        assert db.query(BundleComponent).count() == 0


def test_garbage_component_qty_is_rejected_not_coerced():
    resp = _call(component_qty=["one"])
    assert resp.status_code == 303
    assert "whole" in resp.headers["location"].lower()
    with SessionLocal() as db:
        assert db.query(Bundle).count() == 0


# 6. Blank numerics OK
def test_blank_numerics_default_to_zero_and_qty_one():
    _call(msrp="", selling_price="",
          component_qty=[""], component_msrp=[""], component_cogs=[""])
    with SessionLocal() as db:
        b = db.query(Bundle).one()
        assert b.msrp == Decimal("0")
        assert b.selling_price == Decimal("0")
        c = b.components[0]
        assert c.quantity == 1
        assert c.msrp == Decimal("0")
        assert c.unit_cogs == Decimal("0")


# 7. At least one component required
def test_all_blank_components_is_rejected():
    resp = _call(component_sku=["", "", ""],
                 component_name=["", "", ""],
                 component_qty=["", "", ""],
                 component_msrp=["", "", ""],
                 component_cogs=["", "", ""])
    assert resp.status_code == 303
    assert "component" in resp.headers["location"].lower()
    with SessionLocal() as db:
        assert db.query(Bundle).count() == 0


def test_only_first_row_populated_succeeds():
    """User adds extra empty rows in the UI but only fills one → success;
    only the populated row gets inserted."""
    _call(component_sku=["SBX-ONE", "", ""],
          component_name=["", "", ""],
          component_qty=["", "", ""],
          component_msrp=["", "", ""],
          component_cogs=["", "", ""])
    with SessionLocal() as db:
        b = db.query(Bundle).one()
        assert len(b.components) == 1
        assert b.components[0].component_sku == "SBX-ONE"
