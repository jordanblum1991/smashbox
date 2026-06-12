"""Order-coverage gap detection — days inside the active order range with NO
orders. A gap usually means a missing import (or, once the API is live, a missed
sync), so this is the early-warning that catches under-reported P&L.

Informational by nature (a genuinely slow day can be zero), so it's surfaced on
the recon page but deliberately NOT counted in the nav Data Health badge.
"""
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.order import Order


@dataclass
class CoverageGap:
    start: date          # first missing day
    end: date            # last missing day (inclusive)
    days: int            # length of the run


@dataclass
class CoverageView:
    gaps: list[CoverageGap]
    first_day: date | None
    last_day: date | None
    covered_days: int
    missing_days: int


def compute_order_coverage(db: Session) -> CoverageView:
    """Find runs of consecutive order-free days between the first and last order
    (by placed-at calendar date). Returns the gaps newest-first."""
    placed = db.execute(select(Order.placed_at)).all()
    days = {p.date() for (p,) in placed if p is not None}
    if not days:
        return CoverageView(gaps=[], first_day=None, last_day=None, covered_days=0, missing_days=0)

    first, last = min(days), max(days)
    gaps: list[CoverageGap] = []
    run_start: date | None = None
    d = first
    while d <= last:
        if d in days:
            if run_start is not None:
                prev = d - timedelta(days=1)
                gaps.append(CoverageGap(start=run_start, end=prev, days=(prev - run_start).days + 1))
                run_start = None
        else:
            if run_start is None:
                run_start = d
        d += timedelta(days=1)

    total_span = (last - first).days + 1
    missing = sum(g.days for g in gaps)
    gaps.sort(key=lambda g: g.start, reverse=True)  # newest gaps first
    return CoverageView(
        gaps=gaps, first_day=first, last_day=last,
        covered_days=total_span - missing, missing_days=missing,
    )
