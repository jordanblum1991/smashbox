"""Tests for the inline bundle detail-edit route: POST /admin/bundles/{id}/edit.

The drawer "Save changes" path on the redesigned Manage Bundles page. It edits
the bundle-level entered MSRP + Selling Price only — component rows (and thus
the component-derived calculated_msrp/calculated_cogs) are NOT edited here.

Covers:
  1. Happy path — msrp + selling_price persist; returns the refreshed view row.
  2. Strict validation (reuses create_sku's money parser) — bad number,
     negative value → HTTP 400, row not mutated.
  3. Unknown id → HTTP 404.
  4. Editing bundle-level price does NOT alter components / calculated_* totals.
"""
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.db import Base, SessionLocal, engine
from app.models.bundle import Bundle, BundleComponent
from app.routers.admin import update_bundle_details


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _seed_bundle(with_components=True, **over) -> int:
    with SessionLocal() as db:
        b = Bundle(
            name="Eye Duo",
            variation="Big Flirt + Super Fan",
            brand="Smashbox",
            tiktok_sku_id="9000000000000001234",
            bundle_sku="SBX-BUNDLE-1",
            is_active="Active",
            msrp=Decimal("50.00"),
            selling_price=Decimal("40.00"),
        )
        for k, v in over.items():
            setattr(b, k, v)
        if with_components:
            b.components = [
                BundleComponent(component_sku="SBX-A", component_name="A", quantity=1,
                                msrp=Decimal("25.00"), unit_cogs=Decimal("12.5000")),
                BundleComponent(component_sku="SBX-B", component_name="B", quantity=2,
                                msrp=Decimal("14.00"), unit_cogs=Decimal("7.0000")),
            ]
        db.add(b)
        db.commit()
        return b.id


def _edit(bundle_id, **kwargs):
    defaults = dict(msrp="", selling_price="")
    defaults.update(kwargs)
    with SessionLocal() as db:
        return update_bundle_details(bundle_id=bundle_id, db=db, **defaults)


# 1. Happy path
def test_edit_persists_msrp_and_selling_price():
    bid = _seed_bundle()
    out = _edit(bid, msrp="59.99", selling_price="49.99")
    assert out["ok"] is True
    assert out["bundle"]["msrp"] == 59.99
    assert out["bundle"]["selling_price"] == 49.99
    with SessionLocal() as db:
        b = db.get(Bundle, bid)
        assert b.msrp == Decimal("59.99")
        assert b.selling_price == Decimal("49.99")


def test_blank_prices_store_zero():
    bid = _seed_bundle()
    out = _edit(bid)  # both blank
    assert out["bundle"]["msrp"] == 0.0
    assert out["bundle"]["selling_price"] == 0.0
    with SessionLocal() as db:
        b = db.get(Bundle, bid)
        assert b.msrp == Decimal("0")
        assert b.selling_price == Decimal("0")


# 2. Strict validation
def test_garbage_msrp_is_rejected_not_coerced():
    bid = _seed_bundle()
    with pytest.raises(HTTPException) as ei:
        _edit(bid, msrp="abc", selling_price="10")
    assert ei.value.status_code == 400
    assert "must be a number" in ei.value.detail.lower()
    with SessionLocal() as db:
        assert db.get(Bundle, bid).msrp == Decimal("50.00")  # unchanged


def test_negative_selling_price_is_rejected():
    bid = _seed_bundle()
    with pytest.raises(HTTPException) as ei:
        _edit(bid, msrp="10", selling_price="-3")
    assert ei.value.status_code == 400
    assert "at least 0" in ei.value.detail.lower()
    with SessionLocal() as db:
        assert db.get(Bundle, bid).selling_price == Decimal("40.00")  # unchanged


# 3. Unknown id
def test_unknown_bundle_id_returns_404():
    with pytest.raises(HTTPException) as ei:
        _edit(999999, msrp="5", selling_price="5")
    assert ei.value.status_code == 404


# 4. Components / calculated totals untouched by a bundle-level price edit
def test_price_edit_does_not_touch_components_or_calc_totals():
    bid = _seed_bundle()
    # calc_msrp = 25*1 + 14*2 = 53 ; calc_cogs = 12.5*1 + 7*2 = 26.5
    out = _edit(bid, msrp="99.00", selling_price="80.00")
    assert out["bundle"]["calc_msrp"] == 53.0
    assert out["bundle"]["calc_cogs"] == 26.5
    assert len(out["bundle"]["components"]) == 2
    with SessionLocal() as db:
        b = db.get(Bundle, bid)
        assert len(b.components) == 2
        assert b.calculated_msrp == Decimal("53.00")
        assert b.calculated_cogs == Decimal("26.5000")
