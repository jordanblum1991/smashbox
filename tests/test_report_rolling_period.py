# tests/test_report_rolling_period.py
"""Rolling-window resolver for scheduled report emails — deterministic via a
fixed `today`."""
from datetime import date

from app.services.report_email_common import (
    SALES_PERIODS, SAMPLE_PERIODS, resolve_rolling_period,
)

TODAY = date(2026, 6, 15)   # day <= 28 → current fiscal month == (2026, 6)


def test_prev_month():
    w = resolve_rolling_period("prev_month", today=TODAY)
    assert (w.start, w.end) == (date(2026, 5, 1), date(2026, 5, 31))
    assert w.fiscal_ym is None


def test_mtd():
    w = resolve_rolling_period("mtd", today=TODAY)
    assert (w.start, w.end) == (date(2026, 6, 1), date(2026, 6, 15))


def test_last_7_and_30():
    assert resolve_rolling_period("last_7", today=TODAY).start == date(2026, 6, 9)
    assert resolve_rolling_period("last_7", today=TODAY).end == date(2026, 6, 15)
    assert resolve_rolling_period("last_30", today=TODAY).start == date(2026, 5, 17)


def test_prev_week_is_a_mon_sun_block_before_this_week():
    from datetime import timedelta
    w = resolve_rolling_period("prev_week", today=TODAY)
    assert w.start.weekday() == 0 and w.end.weekday() == 6   # Mon..Sun
    assert (w.end - w.start).days == 6
    this_monday = TODAY - timedelta(days=TODAY.weekday())
    assert w.end < this_monday                               # strictly last week


def test_prev_fiscal_month():
    w = resolve_rolling_period("prev_fiscal_month", today=TODAY)
    # current fiscal (2026,6) → previous fiscal (2026,5) = 29 Apr .. 28 May
    assert w.fiscal_ym == (2026, 5)
    assert (w.start, w.end) == (date(2026, 4, 29), date(2026, 5, 28))


def test_prev_fiscal_month_non_leap_march():
    # today in fiscal April 2027 → previous fiscal March 2027 (non-leap year):
    # Feb has no 29th, so the window must start Mar 1 (not crash).
    w = resolve_rolling_period("prev_fiscal_month", today=date(2027, 4, 15))
    assert w.fiscal_ym == (2027, 3)
    assert (w.start, w.end) == (date(2027, 3, 1), date(2027, 3, 28))


def test_unknown_key_falls_back_to_prev_month():
    w = resolve_rolling_period("bogus", today=TODAY)
    assert (w.start, w.end) == (date(2026, 5, 1), date(2026, 5, 31))


def test_period_allowlists():
    assert "prev_fiscal_month" in SALES_PERIODS
    assert "prev_fiscal_month" not in SAMPLE_PERIODS    # samples is month-granular
    assert SAMPLE_PERIODS == ["prev_month", "mtd"]
