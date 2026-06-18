"""Smashbox fiscal calendar — ONE source of truth for the 29th → 28th periods.

Each fiscal month runs the 29th through the 28th and is LABELED by its closing
month (convention A): Fiscal May 2026 = Apr 29 – May 28; Fiscal Year 2026 =
Dec 29 2025 – Dec 28 2026; Fiscal YTD through M = fiscal Jan..M.

Shared by the P&L (`app/reports/pnl.py`) and the Ad Spend report
(`app/reports/ad_spend.py`) so the window math is never duplicated.
"""
import calendar
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


def _fmt(d: date) -> str:
    """'Apr 29, 2026' — manual day build (avoids platform-specific strftime)."""
    return f"{calendar.month_abbr[d.month]} {d.day}, {d.year}"


def fiscal_label(year: int, month: int, mode: str) -> str:
    """Human label for a fiscal scope: 'Fiscal May 2026' / 'Fiscal YTD through
    May 2026' / 'Fiscal Year 2026'."""
    mlabel = f"{calendar.month_abbr[month]} {year}"
    if mode == "month":
        return f"Fiscal {mlabel}"
    if mode == "ytd":
        return f"Fiscal YTD through {mlabel}"
    if mode == "year":
        return f"Fiscal Year {year}"
    raise ValueError(f"unknown fiscal mode: {mode!r}")


def fiscal_span(year: int, month: int, mode: str) -> tuple[date, date]:
    """(start_inclusive, end_inclusive) covering the whole scope —
    month → that fiscal month; ytd → fiscal Jan..month; year → the 12 months."""
    if mode == "year":
        start, _ = fiscal_window(year, 1)
        _, end = fiscal_window(year, 12)
    elif mode == "ytd":
        start, _ = fiscal_window(year, 1)
        _, end = fiscal_window(year, month)
    else:
        start, end = fiscal_window(year, month)
    return start, end


def fiscal_range_str(year: int, month: int, mode: str) -> str:
    """'Apr 29, 2026 – May 28, 2026' for the scope's full span."""
    start, end = fiscal_span(year, month, mode)
    return f"{_fmt(start)} – {_fmt(end)}"


# Maps a report's period/scope VALUE (string) to a fiscal mode. Keyed by the
# raw string so the P&L (PeriodKind value), Ad Spend (scope), and Dashboard all
# share one definition without importing PeriodKind here.
_BANNER_MODES = {"fiscal_month": "month", "fiscal_ytd": "ytd", "fiscal_year": "year"}


def fiscal_banner_payload(period_value: str, year: int, month: int | None) -> dict | None:
    """{label, range} for the "Fiscal Period" accent banner, or None when the
    scope isn't fiscal (so the banner simply doesn't render)."""
    mode = _BANNER_MODES.get(period_value)
    if mode is None:
        return None
    m = month or 1
    return {"label": fiscal_label(year, m, mode), "range": fiscal_range_str(year, m, mode)}
