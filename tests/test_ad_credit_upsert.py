"""Tests for the ad-credit upsert route.

Documents the Option B persistence contract: saving any amount including $0
records a confirmed entry that persists across reload. Unparseable / blank
amount (or date) is REJECTED via a query-param error redirect, not silently
coerced — silent coercion would write a wrong confirmed-$0 entry, defeating
the whole "saved vs. never-entered" distinction the UI is built on.

The credit is keyed by (year, month) — at most one credit per calendar
month, but the date inside that month is now flexible and editable.
"""
from datetime import date
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


def _call(amount, *, applied_date="2026-03-15", note=None):
    """Invoke the route function directly. Returns (RedirectResponse,
    AdCredit-or-None after the route's own commit). `applied_date` is the
    ISO string the form would post; the (year, month) lookup is derived
    from it."""
    with SessionLocal() as db:
        resp = upsert_ad_credit(
            applied_date=applied_date, amount=amount, note=note, db=db,
        )
    # Derive (year, month) from the input date for the post-write lookup so
    # the helper can return the row the route just upserted. Tolerate a bad
    # date string by skipping the lookup (caller is testing the reject path).
    try:
        d = date.fromisoformat((applied_date or "").strip())
    except ValueError:
        d = None
    with SessionLocal() as db:
        if d is None:
            row = None
        else:
            row = (
                db.query(AdCredit)
                .filter_by(year=d.year, month=d.month)
                .one_or_none()
            )
    return resp, row


# ---------------------------------------------------------------------------
# Happy path: save persists, including $0 and the chosen date
# ---------------------------------------------------------------------------

def test_save_positive_amount_persists():
    resp, row = _call("150.00", note="Q1 makegood")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/reports/ad-spend/reimbursements"
    assert row is not None
    assert row.amount == Decimal("150.00")
    assert row.applied_date == date(2026, 3, 15)
    assert row.year == 2026 and row.month == 3
    assert row.note == "Q1 makegood"


def test_save_zero_persists_row_not_deleted():
    """Load-bearing Option B contract: $0 is a sticky confirmed entry,
    NOT a delete trigger."""
    resp, row = _call("0")
    assert resp.status_code == 303
    assert row is not None, "row should still exist after $0 save"
    assert row.amount == Decimal("0")
    assert row.applied_date == date(2026, 3, 15)


def test_save_zero_then_zero_idempotent():
    _call("0")
    _call("0")
    with SessionLocal() as db:
        rows = db.query(AdCredit).filter_by(year=2026, month=3).all()
        assert len(rows) == 1
        assert rows[0].amount == Decimal("0")


def test_save_zero_after_positive_updates_in_place_does_not_delete():
    _call("100.00")
    _call("0")
    with SessionLocal() as db:
        rows = db.query(AdCredit).filter_by(year=2026, month=3).all()
        assert len(rows) == 1
        assert rows[0].amount == Decimal("0")


def test_resaving_with_a_different_day_in_same_month_updates_the_date():
    """Per-date granularity: re-submitting for the same month with a different
    day overwrites the applied_date on the existing row. The UNIQUE on
    (year, month) means we never get a second row for the same month."""
    _call("100.00", applied_date="2026-03-05")
    _call("100.00", applied_date="2026-03-25")
    with SessionLocal() as db:
        rows = db.query(AdCredit).filter_by(year=2026, month=3).all()
        assert len(rows) == 1
        assert rows[0].applied_date == date(2026, 3, 25)


# ---------------------------------------------------------------------------
# Rejection paths: blank/garbage amount and blank/garbage date
# ---------------------------------------------------------------------------

def test_save_blank_amount_redirects_with_error_no_write():
    resp, row = _call("")
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/reports/ad-spend/reimbursements?error=")
    decoded = unquote_plus(location)
    assert "amount is required" in decoded
    assert "enter 0 to confirm no credit" in decoded
    # Date label uses calendar.month_name + day + year ("March 15, 2026")
    assert "March 15, 2026" in decoded
    assert row is None, "blank submission must NOT write a confirmed-$0 row"


def test_save_whitespace_only_amount_treated_as_blank():
    resp, row = _call("   ")
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    decoded = unquote_plus(resp.headers["location"])
    assert "amount is required" in decoded
    assert row is None


def test_save_garbage_amount_redirects_with_error_no_write():
    resp, row = _call("abc")
    assert resp.status_code == 303
    decoded = unquote_plus(resp.headers["location"])
    assert "'abc' is not a valid number" in decoded
    assert "March 15, 2026" in decoded
    assert row is None


def test_garbage_does_not_overwrite_existing_saved_amount():
    """A rejected submission must leave the prior saved row unchanged."""
    _call("250.00", note="real entry")
    resp, _ = _call("xyz")
    assert resp.status_code == 303
    with SessionLocal() as db:
        row = db.query(AdCredit).filter_by(year=2026, month=3).one()
        assert row.amount == Decimal("250.00")
        assert row.note == "real entry"


def test_save_blank_date_redirects_with_error_no_write():
    resp, _ = _call("100.00", applied_date="")
    assert resp.status_code == 303
    decoded = unquote_plus(resp.headers["location"])
    assert "applied date is required" in decoded
    with SessionLocal() as db:
        assert db.query(AdCredit).count() == 0


def test_save_garbage_date_redirects_with_error_no_write():
    resp, _ = _call("100.00", applied_date="not-a-date")
    assert resp.status_code == 303
    decoded = unquote_plus(resp.headers["location"])
    assert "'not-a-date' is not a valid date" in decoded
    with SessionLocal() as db:
        assert db.query(AdCredit).count() == 0


# ---------------------------------------------------------------------------
# UNIQUE (year, month) — re-saves update in place, distinct months get rows
# ---------------------------------------------------------------------------

def test_second_save_updates_in_place_no_duplicate_row():
    _call("100.00", note="first")
    _call("250.00", note="revised")
    with SessionLocal() as db:
        rows = db.query(AdCredit).filter_by(year=2026, month=3).all()
        assert len(rows) == 1
        assert rows[0].amount == Decimal("250.00")
        assert rows[0].note == "revised"


def test_different_dates_in_different_months_get_separate_rows():
    """The (year, month) UNIQUE means distinct months get distinct rows even
    though the natural key is now a date — each date lives in its month."""
    _call("50.00", applied_date="2026-01-10")
    _call("75.00", applied_date="2026-02-20")
    _call("0",     applied_date="2026-03-15")
    with SessionLocal() as db:
        rows = db.query(AdCredit).order_by(AdCredit.month).all()
        assert len(rows) == 3
        assert [r.month for r in rows] == [1, 2, 3]
        assert [r.applied_date for r in rows] == [
            date(2026, 1, 10), date(2026, 2, 20), date(2026, 3, 15),
        ]
        assert [r.amount for r in rows] == [
            Decimal("50.00"), Decimal("75.00"), Decimal("0"),
        ]
