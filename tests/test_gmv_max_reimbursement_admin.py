"""Tests for the GMV Max Reimbursement admin route.

Mirrors the AdCredit upsert tests but with the GmvMaxReimbursement-specific
rules: amount must be > 0 (null = not yet entered; $0 is meaningless and
rejected, unlike AdCredit which allows $0 as a confirmed-no-credit entry).
Validation uses the reject-garbage discipline: blank or unparseable inputs
produce a 303 with ?error=... flash, no silent coercion.
"""
from decimal import Decimal
from urllib.parse import unquote_plus

import pytest

from app.db import Base, SessionLocal, engine
from app.models import GmvMaxReimbursement
from app.routers.gmv_max_reimbursements import (
    delete_gmv_max_reimbursement,
    upsert_gmv_max_reimbursement,
)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _upsert(amount, *, year="2026", month="5", note=None):
    """Invoke the route function directly. Returns (RedirectResponse,
    GmvMaxReimbursement-or-None after the route's own commit)."""
    with SessionLocal() as db:
        resp = upsert_gmv_max_reimbursement(
            year=year, month=month, amount=amount, note=note, db=db,
        )
    try:
        y = int(year)
        m = int(month)
    except ValueError:
        y, m = None, None
    with SessionLocal() as db:
        if y is None or m is None or not (1 <= m <= 12):
            row = None
        else:
            row = (
                db.query(GmvMaxReimbursement)
                .filter_by(year=y, month=m)
                .one_or_none()
            )
    return resp, row


# ---------------------------------------------------------------------------
# Happy paths: create + update in place
# ---------------------------------------------------------------------------

def test_create_with_positive_amount_persists():
    resp, row = _upsert("1500.00", note="Smashbox confirmed via email 6/2/2026")
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/admin/gmv-max-reimbursements?")
    assert "notice=" in resp.headers["location"]
    assert row is not None
    assert row.amount == Decimal("1500.00")
    assert row.year == 2026 and row.month == 5
    assert row.note == "Smashbox confirmed via email 6/2/2026"


def test_resave_same_month_updates_in_place_no_duplicate_row():
    """The 'edit' UX is just re-submitting the form for the same (year, month).
    UNIQUE prevents duplicates; the route overwrites amount + note."""
    _upsert("1000.00", note="first")
    _upsert("1500.00", note="revised")
    with SessionLocal() as db:
        rows = db.query(GmvMaxReimbursement).filter_by(year=2026, month=5).all()
        assert len(rows) == 1
        assert rows[0].amount == Decimal("1500.00")
        assert rows[0].note == "revised"


def test_different_months_get_separate_rows():
    _upsert("100.00", year="2026", month="3")
    _upsert("200.00", year="2026", month="4")
    _upsert("300.00", year="2026", month="5")
    with SessionLocal() as db:
        rows = db.query(GmvMaxReimbursement).order_by(GmvMaxReimbursement.month).all()
        assert len(rows) == 3
        assert [r.month for r in rows] == [3, 4, 5]
        assert [r.amount for r in rows] == [
            Decimal("100.00"), Decimal("200.00"), Decimal("300.00"),
        ]


# ---------------------------------------------------------------------------
# Amount-validation rejection paths
# ---------------------------------------------------------------------------

def test_blank_amount_rejected_no_write():
    resp, row = _upsert("")
    assert resp.status_code == 303
    decoded = unquote_plus(resp.headers["location"])
    assert "Amount is required" in decoded
    assert row is None


def test_whitespace_only_amount_rejected_as_blank():
    resp, row = _upsert("   ")
    assert resp.status_code == 303
    decoded = unquote_plus(resp.headers["location"])
    assert "Amount is required" in decoded
    assert row is None


def test_garbage_amount_rejected_no_write():
    resp, row = _upsert("abc")
    assert resp.status_code == 303
    decoded = unquote_plus(resp.headers["location"])
    assert "Amount must be a number" in decoded
    assert "'abc'" in decoded
    assert row is None


def test_zero_amount_rejected_no_write():
    """Unlike AdCredit which allows $0 as a confirmed-no-credit entry,
    GmvMaxReimbursement rejects $0 — null = not yet entered; a stored 0
    would be indistinguishable from null on the P&L but would cost a row."""
    resp, row = _upsert("0")
    assert resp.status_code == 303
    decoded = unquote_plus(resp.headers["location"])
    assert "must be greater than 0" in decoded
    assert row is None


def test_negative_amount_rejected_no_write():
    resp, row = _upsert("-50.00")
    assert resp.status_code == 303
    decoded = unquote_plus(resp.headers["location"])
    assert "must be greater than 0" in decoded
    assert row is None


def test_garbage_does_not_overwrite_existing_saved_amount():
    _upsert("1000.00", note="real entry")
    resp, _ = _upsert("xyz")
    assert resp.status_code == 303
    with SessionLocal() as db:
        row = db.query(GmvMaxReimbursement).filter_by(year=2026, month=5).one()
        assert row.amount == Decimal("1000.00")
        assert row.note == "real entry"


# ---------------------------------------------------------------------------
# Year / month validation
# ---------------------------------------------------------------------------

def test_garbage_year_rejected():
    resp, row = _upsert("100.00", year="abc", month="5")
    assert resp.status_code == 303
    decoded = unquote_plus(resp.headers["location"])
    assert "Year must be a number" in decoded
    assert row is None


def test_garbage_month_rejected():
    resp, row = _upsert("100.00", year="2026", month="xyz")
    assert resp.status_code == 303
    decoded = unquote_plus(resp.headers["location"])
    assert "Month must be a number" in decoded
    assert row is None


def test_out_of_range_month_rejected():
    resp, _ = _upsert("100.00", year="2026", month="13")
    assert resp.status_code == 303
    decoded = unquote_plus(resp.headers["location"])
    assert "Month must be 1-12" in decoded


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

def test_delete_removes_row():
    _upsert("100.00")
    with SessionLocal() as db:
        row = db.query(GmvMaxReimbursement).one()
        row_id = row.id
    with SessionLocal() as db:
        resp = delete_gmv_max_reimbursement(row_id=row_id, db=db)
    assert resp.status_code == 303
    with SessionLocal() as db:
        assert db.query(GmvMaxReimbursement).count() == 0


def test_delete_nonexistent_row_raises_404():
    from fastapi import HTTPException
    with SessionLocal() as db:
        with pytest.raises(HTTPException) as exc:
            delete_gmv_max_reimbursement(row_id=99999, db=db)
    assert exc.value.status_code == 404


def test_delete_then_recreate_same_month_works():
    """Deletion clears the unique constraint slot; a new entry for the same
    month afterwards is accepted (regression guard for the UNIQUE / orphan
    interaction)."""
    _upsert("100.00")
    with SessionLocal() as db:
        row_id = db.query(GmvMaxReimbursement).one().id
    with SessionLocal() as db:
        delete_gmv_max_reimbursement(row_id=row_id, db=db)
    resp, row = _upsert("200.00")
    assert resp.status_code == 303
    assert row is not None
    assert row.amount == Decimal("200.00")
