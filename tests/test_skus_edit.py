"""Tests for the inline SKU detail-edit route: POST /admin/skus/{id}/edit.

This is the drawer "Save changes" path on the redesigned Manage SKUs page. It
edits Unit COGS + the five planning fields (lead time, MOQ, case pack, safety
stock %, service level) and is the ONLY inline write on that page.

Covers:
  1. Happy path — all editable fields persist; returns the refreshed view row.
  2. Clearing — blank planning fields → None; blank unit_cogs → 0.
  3. Strict validation (reuses create_sku's parsers) — bad int, safety out of
     range, negative COGS, service level not in the Z-table → HTTP 400, and the
     row is NOT mutated.
  4. Unknown id → HTTP 404.
  5. LOAD-BEARING: editing Unit COGS does NOT touch a historical
     Order.unit_cogs_snapshot (those are frozen at import time).
"""
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.db import Base, SessionLocal, engine
from app.models.import_batch import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.order import Order, OrderLine
from app.models.sku import Sku
from app.routers.admin import update_sku_details


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _seed_sku(**over) -> int:
    """Insert a baseline Sku and return its id. Override fields per test."""
    with SessionLocal() as db:
        s = Sku(
            sku="SBX-EDIT-1",
            name="Edit Target",
            brand="Smashbox",
            msrp=Decimal("50.00"),
            unit_cogs=Decimal("10.0000"),
            is_active=True,
            is_reorderable=True,
        )
        for k, v in over.items():
            setattr(s, k, v)
        db.add(s)
        db.commit()
        return s.id


def _edit(sku_id, **kwargs):
    """Invoke update_sku_details directly with blank defaults; override per test."""
    defaults = dict(
        unit_cogs="",
        lead_time_days="",
        moq="",
        case_pack="",
        safety_stock_pct="",
        service_level="",
    )
    defaults.update(kwargs)
    with SessionLocal() as db:
        return update_sku_details(sku_id=sku_id, db=db, **defaults)


# 1. Happy path
def test_edit_persists_all_editable_fields():
    sid = _seed_sku()
    out = _edit(
        sid,
        unit_cogs="13.25",
        lead_time_days="45",
        moq="500",
        case_pack="24",
        safety_stock_pct="15",
        service_level="0.95",
    )
    assert out["ok"] is True
    # returned view row reflects new values (used to refresh the grid in place)
    assert out["sku"]["unit_cogs"] == 13.25
    assert out["sku"]["lead_time_days"] == 45
    assert out["sku"]["moq"] == 500
    assert out["sku"]["case_pack"] == 24
    assert out["sku"]["safety_stock_pct"] == 15.0
    assert out["sku"]["service_level"] == 0.95
    # and the DB row is updated, COGS at the model's 4-dp precision
    with SessionLocal() as db:
        s = db.get(Sku, sid)
        assert s.unit_cogs == Decimal("13.2500")
        assert s.lead_time_days == 45
        assert s.moq == 500
        assert s.case_pack == 24
        assert s.safety_stock_pct == Decimal("15.00")
        assert s.service_level == Decimal("0.95")


# 2. Clearing
def test_blank_planning_fields_clear_to_none_and_cogs_to_zero():
    sid = _seed_sku(
        unit_cogs=Decimal("9.0000"),
        lead_time_days=30,
        moq=100,
        case_pack=6,
        safety_stock_pct=Decimal("12.00"),
        service_level=Decimal("0.90"),
    )
    out = _edit(sid)  # all blank
    assert out["ok"] is True
    with SessionLocal() as db:
        s = db.get(Sku, sid)
        assert s.lead_time_days is None
        assert s.moq is None
        assert s.case_pack is None
        assert s.safety_stock_pct is None
        assert s.service_level is None
        assert s.unit_cogs == Decimal("0.0000")  # blank money → 0, not None


# 3. Strict validation — bad values rejected, row untouched
def test_garbage_moq_is_rejected_not_coerced():
    sid = _seed_sku(moq=100)
    with pytest.raises(HTTPException) as ei:
        _edit(sid, moq="abc")
    assert ei.value.status_code == 400
    assert "whole number" in ei.value.detail.lower()
    with SessionLocal() as db:
        assert db.get(Sku, sid).moq == 100  # unchanged


def test_safety_stock_above_100_is_rejected():
    sid = _seed_sku()
    with pytest.raises(HTTPException) as ei:
        _edit(sid, safety_stock_pct="250")
    assert ei.value.status_code == 400
    assert "at most 100" in ei.value.detail.lower()


def test_negative_unit_cogs_is_rejected():
    sid = _seed_sku(unit_cogs=Decimal("10.0000"))
    with pytest.raises(HTTPException) as ei:
        _edit(sid, unit_cogs="-5")
    assert ei.value.status_code == 400
    assert "at least 0" in ei.value.detail.lower()
    with SessionLocal() as db:
        assert db.get(Sku, sid).unit_cogs == Decimal("10.0000")  # unchanged


def test_service_level_not_in_table_is_rejected():
    sid = _seed_sku()
    with pytest.raises(HTTPException) as ei:
        _edit(sid, service_level="0.5")
    assert ei.value.status_code == 400
    assert "service level must be one of" in ei.value.detail.lower()


# 4. Unknown id
def test_unknown_sku_id_returns_404():
    with pytest.raises(HTTPException) as ei:
        _edit(999999, unit_cogs="5")
    assert ei.value.status_code == 404


# 5. LOAD-BEARING: editing master COGS must NOT rewrite a frozen snapshot
def test_editing_unit_cogs_does_not_touch_historical_snapshot():
    tiktok_id = "9000000000000007777"
    sid = _seed_sku(sku="SBX-SNAP", tiktok_sku_id=tiktok_id, unit_cogs=Decimal("10.0000"))
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
            tiktok_order_id="TT-SNAP-1",
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
            unit_cogs_snapshot=Decimal("9.9900"),
        ))
        db.commit()

    _edit(sid, unit_cogs="20.00")  # bump the master cost

    with SessionLocal() as db:
        assert db.get(Sku, sid).unit_cogs == Decimal("20.0000")        # master updated
        line = db.query(OrderLine).filter_by(sku=tiktok_id).one()
        assert line.unit_cogs_snapshot == Decimal("9.9900")            # snapshot frozen
