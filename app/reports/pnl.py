"""Unified P&L view — dispatches to the existing monthly/YTD engines.

Three period kinds:

  - `month` : a single calendar month, e.g. May 2026
  - `year`  : Jan..Dec of the given year (per-month columns + totals)
  - `ytd`   : Jan..selected_month of the given year (per-month columns + totals)

No P&L math lives in this module. It composes `compute_monthly_pnl` and
`compute_ytd_pnl` so the line items, settlement coverage, and seller-funded
split logic remain a single source of truth.
"""
from dataclasses import dataclass
from datetime import date
from enum import Enum

from sqlalchemy.orm import Session

from app.reports.monthly_pnl import MonthlyPnL, compute_monthly_pnl
from app.reports.ytd_pnl import YtdPnL, compute_ytd_pnl
from app.templating import month_label


class PeriodKind(str, Enum):
    MONTH = "month"
    YEAR = "year"
    YTD = "ytd"


@dataclass
class PnLView:
    title: str
    period_kind: PeriodKind
    year: int
    month: int | None             # set when period is MONTH or YTD
    total: MonthlyPnL             # the aggregated P&L for the period
    monthly_breakdown: list[MonthlyPnL] | None  # set for YEAR / YTD


def compute_pnl_view(
    db: Session,
    period: PeriodKind,
    year: int,
    month: int | None = None,
) -> PnLView:
    """Resolve the period selector into a PnLView."""
    if period == PeriodKind.MONTH:
        m = month or date.today().month
        pnl = compute_monthly_pnl(db, year, m)
        return PnLView(
            title=f"P&L for {month_label(year, m)}",
            period_kind=period,
            year=year,
            month=m,
            total=pnl,
            monthly_breakdown=None,
        )

    if period == PeriodKind.YEAR:
        ytd = compute_ytd_pnl(db, year, through_month=12)
        return PnLView(
            title=f"P&L for {year}",
            period_kind=period,
            year=year,
            month=None,
            total=ytd.total,
            monthly_breakdown=ytd.months,
        )

    # PeriodKind.YTD
    m = month or date.today().month
    ytd = compute_ytd_pnl(db, year, through_month=m)
    return PnLView(
        title=f"YTD P&L through {month_label(year, m)}",
        period_kind=period,
        year=year,
        month=m,
        total=ytd.total,
        monthly_breakdown=ytd.months,
    )
