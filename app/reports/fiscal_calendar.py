"""Smashbox fiscal calendar — ONE source of truth for the 29th → 28th periods.

Each fiscal month runs the 29th through the 28th and is LABELED by its closing
month (convention A): Fiscal May 2026 = Apr 29 – May 28; Fiscal Year 2026 =
Dec 29 2025 – Dec 28 2026; Fiscal YTD through M = fiscal Jan..M.

Shared by the P&L (`app/reports/pnl.py`) and the Ad Spend report
(`app/reports/ad_spend.py`) so the window math is never duplicated.
"""
from datetime import date, timedelta


def fiscal_window(year: int, month: int) -> tuple[date, date]:
    """(start_inclusive, end_inclusive) for 'Fiscal <month> <year>' — the period
    that CLOSES on the 28th of `month`.

    start = the day after the previous month's 28th, i.e. normally the 29th. The
    one wrinkle is fiscal March in a non-leap year: February has no 29th, so "the
    day after Feb 28" is Mar 1 — handled automatically by date math. The 28th-end
    is always exact, and crossing the year boundary (fiscal January starts Dec 29
    of the prior year) falls out of the prev-month step.
    """
    end_incl = date(year, month, 28)
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    start = date(prev_y, prev_m, 28) + timedelta(days=1)
    return start, end_incl


def fiscal_months_for(mode: str, month: int) -> list[int]:
    """Fiscal month numbers a scope covers:
      'month' → [month]      'ytd' → 1..month      'year' → 1..12
    """
    if mode == "month":
        return [month]
    if mode == "ytd":
        return list(range(1, month + 1))
    if mode == "year":
        return list(range(1, 13))
    raise ValueError(f"unknown fiscal mode: {mode!r}")
