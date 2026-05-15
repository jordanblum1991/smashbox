"""Test the payout cash reconciliation in app/reports/reconciliation.py.

Anchors the assertions: for payouts paid in the period, sum of
Settlement.net_order_margin (via linked_payout_id) should equal Payout.net_amount.
"""
from datetime import datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import (
    ImportBatch,
    ImportBatchStatus,
    ImportFileKind,
    Order,
    OrderType,
    Payout,
    Settlement,
)
from app.reports.reconciliation import reconcile_month


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _batch(db, kind: ImportFileKind, name: str = "f") -> ImportBatch:
    b = ImportBatch(
        kind=kind,
        status=ImportBatchStatus.COMPLETED,
        original_filename=name,
        stored_path=f"/tmp/{name}",
    )
    db.add(b)
    db.flush()
    return b


def _payout(db, batch, payout_id: str, paid_at: datetime, net: str) -> Payout:
    p = Payout(
        import_batch_id=batch.id,
        payout_id=payout_id,
        paid_at=paid_at,
        net_amount=Decimal(net),
    )
    db.add(p)
    return p


def _settlement(
    db, batch, order_id: str, statement_id: str, payout_id: str, net_margin: str
) -> Settlement:
    s = Settlement(
        import_batch_id=batch.id,
        tiktok_order_id=order_id,
        linked_statement_id=statement_id,
        linked_payout_id=payout_id,
        net_order_margin=Decimal(net_margin),
    )
    db.add(s)
    return s


def _line(report, label_starts_with):
    for line in report.lines:
        if line.label.startswith(label_starts_with):
            return line
    raise AssertionError(f"no line starts with {label_starts_with!r}")


def test_payout_line_no_payouts_loaded():
    """No Payout rows for the period → variance is 0 vs 0 with a 'load payouts' hint."""
    with SessionLocal() as db:
        report = reconcile_month(db, 2026, 5)
    line = _line(report, "Payouts")
    assert line.system_calculated == Decimal("0")
    assert line.tiktok_settlement == Decimal("0")
    assert "upload a payouts-income file" in line.likely_cause


def test_payout_line_ties_when_settlements_match():
    """Expected (sum net_order_margin of linked settlements) == Actual (Payout.net_amount)."""
    with SessionLocal() as db:
        pb = _batch(db, ImportFileKind.TIKTOK_PAYOUTS)
        sb = _batch(db, ImportFileKind.TIKTOK_SETTLEMENTS)

        _payout(db, pb, "P-1", datetime(2026, 5, 10), "150.00")
        _settlement(db, sb, "O-1", "S-1", "P-1", "100.00")
        _settlement(db, sb, "O-2", "S-2", "P-1", "50.00")
        db.commit()

        report = reconcile_month(db, 2026, 5)

    line = _line(report, "Payouts")
    assert line.system_calculated == Decimal("150.00")
    assert line.tiktok_settlement == Decimal("150.00")
    assert line.ok
    assert line.likely_cause is None

    assert len(report.payouts) == 1
    row = report.payouts[0]
    assert row.payout_id == "P-1"
    assert row.expected == Decimal("150.00")
    assert row.actual == Decimal("150.00")
    assert row.ok


def test_payout_line_flags_variance():
    """If linked settlements don't sum to the bank transfer, variance is flagged."""
    with SessionLocal() as db:
        pb = _batch(db, ImportFileKind.TIKTOK_PAYOUTS)
        sb = _batch(db, ImportFileKind.TIKTOK_SETTLEMENTS)

        _payout(db, pb, "P-1", datetime(2026, 5, 10), "100.00")
        # Settlements only account for $80 of the $100 payout (reserve / adjustment).
        _settlement(db, sb, "O-1", "S-1", "P-1", "80.00")
        db.commit()

        report = reconcile_month(db, 2026, 5)

    line = _line(report, "Payouts")
    assert line.variance == Decimal("-20.00")
    assert not line.ok
    assert "less than" in line.likely_cause

    assert len(report.payouts) == 1
    assert not report.payouts[0].ok
    assert report.payouts[0].variance == Decimal("-20.00")


def test_payouts_outside_period_are_excluded():
    """A payout in April shouldn't appear in May's reconciliation."""
    with SessionLocal() as db:
        pb = _batch(db, ImportFileKind.TIKTOK_PAYOUTS)
        sb = _batch(db, ImportFileKind.TIKTOK_SETTLEMENTS)

        _payout(db, pb, "P-APR", datetime(2026, 4, 28), "75.00")
        _settlement(db, sb, "O-APR", "S-APR", "P-APR", "75.00")

        _payout(db, pb, "P-MAY", datetime(2026, 5, 5), "200.00")
        _settlement(db, sb, "O-MAY", "S-MAY", "P-MAY", "200.00")
        db.commit()

        report = reconcile_month(db, 2026, 5)

    line = _line(report, "Payouts")
    assert line.tiktok_settlement == Decimal("200.00")
    assert [r.payout_id for r in report.payouts] == ["P-MAY"]


def test_payouts_sorted_by_paid_at():
    """Drill-down rows are ordered by paid_at so the user can scan a timeline."""
    with SessionLocal() as db:
        pb = _batch(db, ImportFileKind.TIKTOK_PAYOUTS)
        _payout(db, pb, "P-LATE", datetime(2026, 5, 20), "100")
        _payout(db, pb, "P-EARLY", datetime(2026, 5, 1), "200")
        db.commit()

        report = reconcile_month(db, 2026, 5)

    assert [r.payout_id for r in report.payouts] == ["P-EARLY", "P-LATE"]
