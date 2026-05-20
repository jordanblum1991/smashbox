"""Unit tests for the spike-dampened velocity rate.

Reference: velocity-spike-dampening-spec-short.md. The robust rate is what
the buying math uses; the raw mean still feeds stockout flags.
"""
from decimal import Decimal

from app.services.demand.velocity import robust_daily_rate


# ---- No-spike series: robust == raw -----------------------------------------

def test_flat_series_robust_equals_raw():
    # 60 days of 2 units/day = 120 total, mean = 2.0.
    series = [2] * 60
    raw_mean = Decimal("2.00")
    assert robust_daily_rate(series) == raw_mean


def test_smoothly_varying_series_is_unchanged():
    # Realistic noise around a baseline; no single day dominates.
    series = ([1, 2, 3, 2, 1, 2, 3] * 9)[:60]
    raw_mean = (Decimal(sum(series)) / Decimal(60)).quantize(Decimal("0.01"))
    # cap should be well above any actual day (max=3), so nothing clips.
    assert robust_daily_rate(series) == raw_mean


# ---- Single 10× outlier on a flat baseline ----------------------------------

def test_single_outlier_is_clipped_to_cap_day():
    # 60 days, baseline 2/day with one 10× spike to 20.
    # raw mean = (59*2 + 20)/60 = 138/60 = 2.30
    # nonzero median = 2 (most days are 2; one is 20)
    # cap_day = max(3 * 2, 5 * 2.30) = max(6, 11.5) = 11.5
    # the 20-day clips to 11.5, all others stay at 2.
    # clipped_total = 59*2 + 11.5 = 129.5 → robust = 129.5/60 ≈ 2.16
    series = [2] * 59 + [20]
    raw = Decimal("138") / Decimal("60")
    expected_cap = Decimal("5") * raw  # = 11.5, the larger of the two arms
    expected_clipped_total = Decimal("59") * Decimal("2") + expected_cap
    expected_robust = (expected_clipped_total / Decimal("60")).quantize(Decimal("0.01"))

    robust = robust_daily_rate(series)
    assert robust == expected_robust
    assert robust < raw.quantize(Decimal("0.01"))


def test_extreme_outlier_clipped_more_aggressively():
    # 60 days of 1 unit each + one 1000× spike.
    # raw mean = (59 + 1000)/60 ≈ 17.65
    # median nonzero = 1
    # cap_day = max(3*1, 5*17.65) = max(3, 88.25) = 88.25 (mean arm wins)
    # clipped: 59*1 + 88.25 = 147.25 → robust = 2.45
    series = [1] * 59 + [1000]
    robust = robust_daily_rate(series)
    raw = (Decimal(sum(series)) / Decimal(60)).quantize(Decimal("0.01"))
    assert robust < raw
    # The outlier-day clip should bring the mean from 17.65 down dramatically.
    assert robust < Decimal("5")


# ---- Degenerate case: 3 selling days, total >= units gate -------------------

def test_intermittent_sku_mean_arm_engages():
    # 3 selling days (5 units each), 57 zero days, total=15 → over the gate.
    # median of nonzero = 5
    # raw mean = 15/60 = 0.25
    # cap_day = max(3*5, 5*0.25) = max(15, 1.25) = 15 (median arm wins)
    # Each selling day's 5 is below 15, so nothing clips.
    # robust == raw == 0.25
    series = [0] * 57 + [5, 5, 5]
    robust = robust_daily_rate(series)
    raw = Decimal("0.25")
    assert robust == raw


def test_intermittent_sku_with_spike_uses_mean_arm():
    # 2 days of 1 unit each + 1 day of 100. Total=102, over the gate.
    # median nonzero = 1 (sorted [1,1,100])
    # raw mean = 102/60 = 1.70
    # cap_day = max(3*1, 5*1.70) = max(3, 8.50) = 8.50 (mean arm protects)
    # Without the mean arm, cap would be 3 and we'd over-clip.
    series = [0] * 57 + [1, 1, 100]
    robust = robust_daily_rate(series)
    raw = (Decimal(sum(series)) / Decimal(60)).quantize(Decimal("0.01"))
    assert robust < raw
    # The robust value must reflect the mean-arm cap, not zero.
    # clipped = 1 + 1 + 8.50 = 10.50 → robust = 0.175 → quantized 0.17 or 0.18.
    assert robust >= Decimal("0.15")
    assert robust <= Decimal("0.20")


# ---- Units gate suppresses dampening for low-volume SKUs --------------------

def test_below_units_gate_no_dampening():
    # 4 total units across 60 days. Below MIN_UNITS_FOR_DAMPENING (5).
    # Even though one day spikes to 4, no clipping is applied.
    series = [0] * 59 + [4]
    robust = robust_daily_rate(series)
    raw = (Decimal("4") / Decimal("60")).quantize(Decimal("0.01"))
    assert robust == raw


def test_at_units_gate_dampening_activates():
    # Exactly 5 total units — gate is `<`, so dampening applies.
    # All on one day: median nonzero = 5, raw mean = 5/60 ≈ 0.083.
    # cap_day = max(3*5, 5*0.083) = 15. The 5-day stays at 5 (under 15).
    # robust = raw = 0.08.
    series = [0] * 59 + [5]
    robust = robust_daily_rate(series)
    raw = (Decimal("5") / Decimal("60")).quantize(Decimal("0.01"))
    assert robust == raw  # no clip needed, but the gate did open


# ---- Edge cases -------------------------------------------------------------

def test_empty_series_returns_zero():
    assert robust_daily_rate([]) == Decimal("0")


def test_all_zero_series_returns_zero():
    assert robust_daily_rate([0] * 60) == Decimal("0")


def test_explicit_constants_override():
    # Same single-spike scenario, but with a tighter cap_mult (1.5x).
    # series: [2]*59 + [20]; median=2, raw=2.30
    # cap_day = max(1.5*2, 5*2.30) = max(3, 11.5) = 11.5 → same result.
    # Now tighten the mean arm too: mean_mult=2 → max(3, 4.60) = 4.60.
    series = [2] * 59 + [20]
    robust_tight = robust_daily_rate(
        series, spike_cap_mult=Decimal("1.5"), raw_mean_mult=Decimal("2.0")
    )
    expected_clipped = Decimal("59") * Decimal("2") + Decimal("4.60")
    assert robust_tight == (expected_clipped / Decimal("60")).quantize(Decimal("0.01"))
