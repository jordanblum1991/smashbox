"""Tests for the shop-local-time reporting helper (Seller Center tie-out).

Pins the validated conversion model: placed_at is UTC-6, bucketed in Pacific.
-1h in PDT (summer), -2h in PST (winter), DST handled by ZoneInfo.
"""
from datetime import date, datetime

from app.services.reporting_tz import (
    placed_local,
    placed_local_date,
    shop_boundary_to_source,
)


# ---- per-row local day (daily bucketing) ----------------------------------

def test_pdt_summer_shifts_minus_one_hour():
    # Apr 15 00:30 source (UTC-6) -> Apr 14 23:30 Pacific (PDT) -> Apr 14.
    assert placed_local(datetime(2026, 4, 15, 0, 30)) \
        .replace(tzinfo=None) == datetime(2026, 4, 14, 23, 30)
    assert placed_local_date(datetime(2026, 4, 15, 0, 30)) == date(2026, 4, 14)


def test_pst_winter_shifts_minus_two_hours():
    # Jan 15 01:30 source -> Jan 14 23:30 Pacific (PST) -> Jan 14.
    assert placed_local(datetime(2026, 1, 15, 1, 30)) \
        .replace(tzinfo=None) == datetime(2026, 1, 14, 23, 30)
    assert placed_local_date(datetime(2026, 1, 15, 1, 30)) == date(2026, 1, 14)


def test_midday_order_stays_same_day():
    # A safely-mid-day order doesn't flip days under a 1-2h shift.
    assert placed_local_date(datetime(2026, 4, 15, 12, 0)) == date(2026, 4, 15)


def test_dst_boundary_within_march():
    # March straddles the 2026-03-08 DST change: an early-March order uses PST
    # (-2h), a late-March order uses PDT (-1h). A constant offset would be wrong.
    assert placed_local_date(datetime(2026, 3, 5, 1, 30)) == date(2026, 3, 4)   # PST -2h
    assert placed_local_date(datetime(2026, 3, 25, 0, 30)) == date(2026, 3, 24)  # PDT -1h


# ---- window boundary conversion (period filters) --------------------------

def test_march_boundary_to_source_is_pst_plus_two():
    # Mar 1 00:00 Pacific (PST, UTC-8) -> Mar 1 02:00 source (UTC-6).
    assert shop_boundary_to_source(datetime(2026, 3, 1)) == datetime(2026, 3, 1, 2, 0)


def test_april_boundary_to_source_is_pdt_plus_one():
    # Apr 1 00:00 Pacific (PDT, UTC-7) -> Apr 1 01:00 source (UTC-6).
    assert shop_boundary_to_source(datetime(2026, 4, 1)) == datetime(2026, 4, 1, 1, 0)


def test_boundary_window_selects_seller_center_month():
    # An order at Apr 1 00:30 source has Pacific date Mar 31 (PDT -1h), so it
    # belongs to Seller Center's MARCH, not April. The converted April-window
    # start (Apr 1 01:00 source) correctly excludes it.
    order_src = datetime(2026, 4, 1, 0, 30)
    assert placed_local_date(order_src) == date(2026, 3, 31)
    assert order_src < shop_boundary_to_source(datetime(2026, 4, 1))   # excluded from April
    assert order_src >= shop_boundary_to_source(datetime(2026, 3, 1))  # included in March
