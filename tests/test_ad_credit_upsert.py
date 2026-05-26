"""Tests for the ad-credit upsert route.

Documents the Option B persistence contract: saving any amount including $0
records a confirmed entry that persists across reload. Unparseable / blank
amount is REJECTED via a query-param error redirect, not silently coerced
to 0 — silent coercion would write a wrong confirmed-$0 entry, defeating
the whole "saved vs. never-entered" distinction the UI is built on.
"""
from decimal import Decimal
from urllib.parse import unquote_plus

import pytest

from app.db import Base, SessionLocal, engine
from app.models import AdCredit
from app.routers.reports import upsert_ad_credit


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _call(amount, *, year=2026, month=3, note=None):
    """Invoke the route function directly. Returns (RedirectResponse, AdCredit-or-None
    after the route's own commit)."""
    with SessionLocal() as db:
        resp = upsert_ad_credit(year=year, month=month, amount=amount, note=note, db=db)
    with SessionLocal() as db:
        row = (
            db.query(AdCredit)
            .filter_by(year=year, month=month)
            .one_or_none()
        )
    return resp, row


# ---------------------------------------------------------------------------
# Happy path: save persists, including $0
# ---------------------------------------------------------------------------

def test_save_positive_amount_persists():
    resp, row = _call("150.00", note="Q1 makegood")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/reports/ad-spend"
    assert row is not None
    assert row.amount == Decimal("150.00")
    assert row.note == "Q1 makegood"


def test_save_zero_persists_row_not_deleted():
    """Load-bearing Option B contract: $0 is a sticky confirmed entry,
    NOT a delete trigger. A separate 'never entered' state is preserved by
    the absence of any row."""
    resp, row = _call("0")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/reports/ad-spend"
    assert row is not None, "row should still exist after $0 save"
    assert row.amount == Decimal("0")


def test_save_zero_then_subsequent_save_zero_still_persists():
    """Saving $0 twice on the same month is idempotent — row stays at 0."""
    _call("0")
    _call("0")
    with SessionLocal() as db:
        rows = db.query(AdCredit).filter_by(year=2026, month=3).all()
        assert len(rows) == 1
        assert rows[0].amount == Decimal("0")


def test_save_zero_after_save_positive_updates_in_place_does_not_delete():
    """Specifically: saving $0 over a previously-saved positive amount UPDATES
    the row to 0, it does NOT delete the row. This is the exact behaviour that
    differs from the pre-Option-B contract."""
    _call("100.00")
    _call("0")
    with SessionLocal() as db:
        rows = db.query(AdCredit).filter_by(year=2026, month=3).all()
        assert len(rows) == 1
        assert rows[0].amount == Decimal("0")


# ---------------------------------------------------------------------------
# Rejection paths: blank and garbage amounts
# ---------------------------------------------------------------------------

def test_save_blank_amount_redirects_with_error_no_write():
    resp, row = _call("")
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/reports/ad-spend?error=")
    decoded = unquote_plus(location)
    assert "amount is required" in decoded
    assert "enter 0 to confirm no credit" in decoded
    assert "March 2026" in decoded
    assert row is None, "blank submission must NOT write a confirmed-$0 row"


def test_save_whitespace_only_amount_treated_as_blank():
    """Whitespace-only is functionally blank — same error path."""
    resp, row = _call("   ")
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    decoded = unquote_plus(resp.headers["location"])
    assert "amount is required" in decoded
    assert row is None


def test_save_garbage_amount_redirects_with_error_no_write():
    resp, row = _call("abc")
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/reports/ad-spend?error=")
    decoded = unquote_plus(location)
    assert "'abc' is not a valid number" in decoded
    assert "March 2026" in decoded
    assert row is None, "garbage submission must NOT write a confirmed-$0 row"


def test_garbage_does_not_overwrite_existing_saved_amount():
    """A rejected submission must leave the prior saved row unchanged —
    the user shouldn't lose a real entry because of a typo."""
    _call("250.00", note="real entry")
    resp, row = _call("xyz")
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    assert row is not None
    assert row.amount == Decimal("250.00")
    assert row.note == "real entry"


# ---------------------------------------------------------------------------
# UNIQUE (year, month) — re-saves update in place
# ---------------------------------------------------------------------------

def test_second_save_updates_in_place_no_duplicate_row():
    _call("100.00", note="first")
    _call("250.00", note="revised")
    with SessionLocal() as db:
        rows = db.query(AdCredit).filter_by(year=2026, month=3).all()
        assert len(rows) == 1
        assert rows[0].amount == Decimal("250.00")
        assert rows[0].note == "revised"


def test_different_months_get_separate_rows():
    """Year/month is the natural key — different months should NOT collide."""
    _call("50.00", year=2026, month=1)
    _call("75.00", year=2026, month=2)
    _call("0", year=2026, month=3)
    with SessionLocal() as db:
        rows = db.query(AdCredit).order_by(AdCredit.month).all()
        assert len(rows) == 3
        assert [r.month for r in rows] == [1, 2, 3]
        assert [r.amount for r in rows] == [Decimal("50.00"), Decimal("75.00"), Decimal("0")]
