"""Unified P&L view — dispatches to the existing monthly/YTD engines.

Four period kinds:

  - `month` : a single calendar month, e.g. May 2026
  - `ytd`   : Jan..selected_month of the given year (per-month columns + totals)
  - `year`  : Jan..Dec of the given year (per-month columns + totals)
  - `range` : an arbitrary [start_month, end_month] window in the same or
              different years (per-month columns + totals)

No P&L math lives here. compute_monthly_pnl is the single source of truth and
every mode is just "sum across the months in this window."
"""
import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum

from sqlalchemy.orm import Session

from app.reports.fiscal_calendar import fiscal_window
from app.reports.monthly_pnl import (
    MonthlyPnL,
    _add_month,
    compute_monthly_pnl,
    compute_window_pnl,
)
from app.reports.ytd_pnl import _sum  # private but ours
from app.services.reporting_tz import today_local
from app.templating import month_label


class PeriodKind(str, Enum):
    MONTH = "month"
    YTD = "ytd"
    YEAR = "year"
    RANGE = "range"
    CUSTOM = "custom"          # arbitrary [start_date, end_date] day window
    # Smashbox fiscal calendar: each fiscal month runs the 29th → 28th and is
    # LABELED by its closing month (convention A). Fiscal May 2026 = Apr 29 –
    # May 28; Fiscal Year 2026 = Dec 29 2025 – Dec 28 2026.
    FISCAL_MONTH = "fiscal_month"
    FISCAL_YTD = "fiscal_ytd"
    FISCAL_YEAR = "fiscal_year"


@dataclass
class PnLView:
    title_suffix: str           # "May 2026" / "YTD through May 2026" / "2026" / "Mar 2026 – May 2026"
    period_kind: PeriodKind
    year: int
    month: int | None
    total: MonthlyPnL
    monthly_breakdown: list[MonthlyPnL] | None  # None for MONTH/CUSTOM, set otherwise

    # CUSTOM mode only — None in every other mode. Populated so the template
    # can repaint the date pickers on reload.
    custom_start: datetime | None = None              # exclusive-end window start
    custom_end: datetime | None = None                # exclusive end (start_of_day_after)
    inclusive_end_date: date | None = None            # the date the user originally picked

    @property
    def title(self) -> str:
        # P&L page uses 'P&L for ...' / 'YTD P&L through ...' phrasing; the
        # dashboard route prepends 'Dashboard: <title_suffix>' instead.
        if self.period_kind == PeriodKind.YTD:
            return f"YTD P&L through {self.title_suffix.replace('YTD through ', '')}"
        return f"P&L for {self.title_suffix}"


def _months_in_range(sy: int, sm: int, ey: int, em: int) -> list[tuple[int, int]]:
    """Inclusive list of (year, month) tuples from (sy,sm) up to (ey,em)."""
    out: list[tuple[int, int]] = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        out.append((y, m))
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1
    return out


def _format_date_short(d: date) -> str:
    """Date suffix for the CUSTOM-mode title: 'Mar 28, 2026'. Manual build
    because %-d / %#d differ across platforms; avoid strftime for the day."""
    return f"{calendar.month_abbr[d.month]} {d.day}, {d.year}"


# ---- Smashbox fiscal calendar (29th → 28th, labeled by closing month) -------
# Window math lives in app/reports/fiscal_calendar.py (shared with Ad Spend).

def _fiscal_window(year: int, month: int) -> tuple[date, date]:
    """Back-compat shim — see app.reports.fiscal_calendar.fiscal_window."""
    return fiscal_window(year, month)


def _fiscal_month_pnl(db: Session, year: int, month: int) -> MonthlyPnL:
    """One fiscal month's P&L over its [start, end) window. Anchored to the
    closing month (date(year, month, 1)) so a multi-month breakdown labels the
    column by the fiscal label — e.g. 'May' for the Apr 29 – May 28 period."""
    start, end_incl = _fiscal_window(year, month)
    return compute_window_pnl(
        db,
        datetime(start.year, start.month, start.day),
        datetime(end_incl.year, end_incl.month, end_incl.day) + timedelta(days=1),  # exclusive
        month_anchor=date(year, month, 1),
    )


def compute_pnl_view(
    db: Session,
    period: PeriodKind,
    year: int | None = None,
    month: int | None = None,
    *,
    start_year: int | None = None,
    start_month: int | None = None,
    end_year: int | None = None,
    end_month: int | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> PnLView:
    """Resolve the selector into a PnLView."""
    today = today_local()
    y = year or today.year
    m = month or today.month

    if period == PeriodKind.MONTH:
        pnl = compute_monthly_pnl(db, y, m)
        return PnLView(
            title_suffix=month_label(y, m),
            period_kind=period,
            year=y, month=m,
            total=pnl,
            monthly_breakdown=None,
        )

    if period == PeriodKind.YTD:
        months_list = [compute_monthly_pnl(db, y, mm) for mm in range(1, m + 1)]
        return PnLView(
            title_suffix=f"YTD through {month_label(y, m)}",
            period_kind=period,
            year=y, month=m,
            total=_sum(months_list, y),
            monthly_breakdown=months_list,
        )

    if period == PeriodKind.YEAR:
        months_list = [compute_monthly_pnl(db, y, mm) for mm in range(1, 13)]
        return PnLView(
            title_suffix=str(y),
            period_kind=period,
            year=y, month=None,
            total=_sum(months_list, y),
            monthly_breakdown=months_list,
        )

    if period == PeriodKind.FISCAL_MONTH:
        start, end_incl = _fiscal_window(y, m)
        return PnLView(
            title_suffix=f"Fiscal {month_label(y, m)} "
                         f"({_format_date_short(start)} – {_format_date_short(end_incl)})",
            period_kind=period, year=y, month=m,
            total=_fiscal_month_pnl(db, y, m),
            monthly_breakdown=None,
        )

    if period == PeriodKind.FISCAL_YTD:
        months_list = [_fiscal_month_pnl(db, y, mm) for mm in range(1, m + 1)]
        fy_start, _ = _fiscal_window(y, 1)
        _, end_incl = _fiscal_window(y, m)
        return PnLView(
            title_suffix=f"Fiscal YTD through {month_label(y, m)} "
                         f"({_format_date_short(fy_start)} – {_format_date_short(end_incl)})",
            period_kind=period, year=y, month=m,
            total=_sum(months_list, y),
            monthly_breakdown=months_list,
        )

    if period == PeriodKind.FISCAL_YEAR:
        months_list = [_fiscal_month_pnl(db, y, mm) for mm in range(1, 13)]
        fy_start, _ = _fiscal_window(y, 1)
        _, fy_end = _fiscal_window(y, 12)
        return PnLView(
            title_suffix=f"Fiscal Year {y} "
                         f"({_format_date_short(fy_start)} – {_format_date_short(fy_end)})",
            period_kind=period, year=y, month=None,
            total=_sum(months_list, y),
            monthly_breakdown=months_list,
        )

    if period == PeriodKind.CUSTOM:
        if start_date is None or end_date is None:
            raise ValueError("CUSTOM period requires both start_date and end_date")
        if start_date > end_date:
            raise ValueError("start_date must be <= end_date")
        start = datetime(start_date.year, start_date.month, start_date.day)
        # User-facing end is inclusive ("through April 27"); window math wants
        # exclusive, so bump by one day to cover all of the chosen end_date.
        end = datetime(end_date.year, end_date.month, end_date.day) + timedelta(days=1)
        pnl = compute_window_pnl(db, start, end, month_anchor=start.date())
        suffix = f"{_format_date_short(start_date)} – {_format_date_short(end_date)}"
        return PnLView(
            title_suffix=suffix,
            period_kind=period,
            year=start_date.year,
            month=start_date.month,
            total=pnl,
            monthly_breakdown=None,                 # single combined column
            custom_start=start,
            custom_end=end,
            inclusive_end_date=end_date,
        )

    # PeriodKind.RANGE
    sy = start_year or y
    sm = start_month or m
    ey = end_year or y
    em = end_month or m
    if (ey, em) < (sy, sm):
        sy, sm, ey, em = ey, em, sy, sm  # silent swap, less surprising than 500ing
    months_pairs = _months_in_range(sy, sm, ey, em)
    months_list = [compute_monthly_pnl(db, py, pm) for py, pm in months_pairs]
    if (sy, sm) == (ey, em):
        suffix = month_label(sy, sm)
    else:
        suffix = f"{month_label(sy, sm)} – {month_label(ey, em)}"
    return PnLView(
        title_suffix=suffix,
        period_kind=period,
        year=sy, month=sm,
        total=_sum(months_list, sy),
        monthly_breakdown=months_list,
    )


def window_for(view: PnLView) -> tuple[datetime, datetime]:
    """Return the [start, end) datetime window matching the view's period —
    so anything that queries Order.placed_at uses the same range the P&L did."""
    if view.period_kind == PeriodKind.CUSTOM and view.custom_start and view.custom_end:
        return view.custom_start, view.custom_end
    if view.period_kind == PeriodKind.MONTH:
        start = datetime(view.year, view.month, 1)
        return start, _add_month(start)
    if view.period_kind in (PeriodKind.FISCAL_MONTH, PeriodKind.FISCAL_YTD,
                            PeriodKind.FISCAL_YEAR):
        # Fiscal windows can't be derived from the (calendar-anchored) breakdown,
        # so resolve them directly. YTD/Year both open at the fiscal year start.
        fy_start, _ = _fiscal_window(view.year, 1)
        if view.period_kind == PeriodKind.FISCAL_MONTH:
            fy_start, _ = _fiscal_window(view.year, view.month)
        end_month = view.month if view.period_kind != PeriodKind.FISCAL_YEAR else 12
        _, end_incl = _fiscal_window(view.year, end_month)
        return (
            datetime(fy_start.year, fy_start.month, fy_start.day),
            datetime(end_incl.year, end_incl.month, end_incl.day) + timedelta(days=1),
        )
    months = view.monthly_breakdown or []
    first = months[0].month
    last = months[-1].month
    start = datetime(first.year, first.month, 1)
    end = _add_month(datetime(last.year, last.month, 1))
    return start, end
