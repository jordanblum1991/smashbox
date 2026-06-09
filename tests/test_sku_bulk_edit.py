"""Bulk-edit endpoint for /admin/skus — apply planning fields + Reorderable to
many SKUs at once. Per-field 'apply' flags: only checked fields change; checked
+ blank clears a nullable. Strict parsers reject bad values with no partial
write; identity/MSRP/COGS are out of scope.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.db import Base, SessionLocal, engine
from app.models.sku import Sku
from app.routers.admin import bulk_edit_skus


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _sku(db, sku_code, **over):
    defaults = dict(sku=sku_code, name="P " + sku_code, brand="B",
                    is_active=True, is_reorderable=True)
    defaults.update(over)
    s = Sku(**defaults)
    db.add(s); db.flush()
    return s


def _call(db, sku_ids, **kw):
    base = dict(
        apply_lead_time_days=False, lead_time_days="",
        apply_moq=False, moq="",
        apply_case_pack=False, case_pack="",
        apply_safety_stock_pct=False, safety_stock_pct="",
        apply_service_level=False, service_level="",
        apply_is_reorderable=False, is_reorderable="",
    )
    base.update(kw)
    return bulk_edit_skus(sku_ids=sku_ids, db=db, **base)


def test_applies_checked_fields_to_all_selected():
    with SessionLocal() as db:
        a, b = _sku(db, "A"), _sku(db, "B")
        db.commit()
        ids = f"{a.id},{b.id}"
        res = _call(db, ids,
                    apply_lead_time_days=True, lead_time_days="14",
                    apply_moq=True, moq="50",
                    apply_service_level=True, service_level="0.95")
        db.commit()
        assert res["ok"] and res["updated_count"] == 2
        for s in (db.get(Sku, a.id), db.get(Sku, b.id)):
            assert s.lead_time_days == 14
            assert s.moq == 50
            assert s.service_level == Decimal("0.95")
            assert s.case_pack is None and s.safety_stock_pct is None  # unchecked → untouched


def test_unchecked_fields_left_unchanged():
    with SessionLocal() as db:
        a = _sku(db, "A", lead_time_days=7, moq=3)
        db.commit()
        _call(db, str(a.id), apply_moq=True, moq="20")  # only MOQ checked
        db.commit()
        s = db.get(Sku, a.id)
        assert s.moq == 20
        assert s.lead_time_days == 7  # untouched


def test_checked_blank_clears_nullable():
    with SessionLocal() as db:
        a = _sku(db, "A", lead_time_days=7)
        db.commit()
        _call(db, str(a.id), apply_lead_time_days=True, lead_time_days="")  # checked + blank → clear
        db.commit()
        assert db.get(Sku, a.id).lead_time_days is None


def test_is_reorderable_bulk_set():
    with SessionLocal() as db:
        a, b = _sku(db, "A", is_reorderable=True), _sku(db, "B", is_reorderable=True)
        db.commit()
        _call(db, f"{a.id},{b.id}", apply_is_reorderable=True, is_reorderable="false")
        db.commit()
        assert db.get(Sku, a.id).is_reorderable is False
        assert db.get(Sku, b.id).is_reorderable is False


def test_bad_value_rejected_no_partial_write():
    with SessionLocal() as db:
        a = _sku(db, "A", moq=5)
        db.commit()
        with pytest.raises(HTTPException) as ei:
            _call(db, str(a.id),
                  apply_lead_time_days=True, lead_time_days="14",  # valid
                  apply_moq=True, moq="abc")                       # invalid
        db.rollback()
        assert ei.value.status_code == 400
        s = db.get(Sku, a.id)
        assert s.moq == 5 and s.lead_time_days is None  # nothing written


def test_no_fields_selected_rejected():
    with SessionLocal() as db:
        a = _sku(db, "A")
        db.commit()
        with pytest.raises(HTTPException) as ei:
            _call(db, str(a.id))
        assert ei.value.status_code == 400


def test_no_skus_selected_rejected():
    with SessionLocal() as db:
        with pytest.raises(HTTPException) as ei:
            _call(db, "", apply_moq=True, moq="10")
        assert ei.value.status_code == 400


def test_missing_sku_404():
    with SessionLocal() as db:
        a = _sku(db, "A")
        db.commit()
        with pytest.raises(HTTPException) as ei:
            _call(db, f"{a.id},999999", apply_moq=True, moq="10")
        assert ei.value.status_code == 404
