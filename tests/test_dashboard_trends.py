"""Unit tests for the dashboard trend helpers — MoM deltas and inline-SVG
sparkline coordinate normalization. These are pure functions (no DB), so the
edge-case guards the spec calls out can be exercised directly:

  - zero / missing prior month  -> "new" or "—", NEVER +inf% or a divide-by-zero
  - flat / rounds-to-zero        -> 0.0% with a neutral 'flat' state
  - negative-base metrics        -> direction stays intuitive (loss shrinking = up)
  - all-equal / empty sparkline  -> no divide-by-zero on min==max
"""
from decimal import Decimal

from app.reports.dashboard_trends import (
    Bar,
    BarChart,
    bar_chart,
    Delta,
    compute_delta,
    sparkline_points,
    trailing_months,
)


D = Decimal


# --------------------------------------------------------------------------- #
# compute_delta — the load-bearing math
# --------------------------------------------------------------------------- #
def test_positive_change_is_up_with_signed_pct():
    d = compute_delta(D("110"), D("100"), prior_has_data=True)
    assert d.state == "up"
    assert d.pct == D("10.0")
    assert d.label == "+10.0%"


def test_negative_change_is_down_with_signed_pct():
    d = compute_delta(D("90"), D("100"), prior_has_data=True)
    assert d.state == "down"
    assert d.pct == D("-10.0")
    assert d.label == "-10.0%"


def test_exactly_equal_is_flat():
    d = compute_delta(D("100"), D("100"), prior_has_data=True)
    assert d.state == "flat"
    assert d.pct == D("0.0")
    assert d.label == "0.0%"


def test_change_rounding_to_zero_is_flat_not_a_signed_arrow():
    # 0.04% rounds to 0.0 — should read as flat, not "▲ +0.0%".
    d = compute_delta(D("100.04"), D("100"), prior_has_data=True)
    assert d.state == "flat"
    assert d.label == "0.0%"


def test_missing_prior_renders_new_never_errors():
    d = compute_delta(D("100"), None, prior_has_data=False)
    assert d.state == "new"
    assert d.label == "new"
    assert d.pct is None


def test_zero_prior_with_nonzero_current_is_new_not_infinite():
    # The classic +inf% trap: prior month had activity but this metric was 0.
    d = compute_delta(D("100"), D("0"), prior_has_data=True)
    assert d.state == "new"
    assert d.label == "new"
    assert d.pct is None


def test_both_zero_is_dash_no_division():
    d = compute_delta(D("0"), D("0"), prior_has_data=True)
    assert d.state == "flat"
    assert d.label == "—"
    assert d.pct is None


def test_loss_shrinking_reads_as_up():
    # Net profit went from -100 to -50: an improvement. Denominator uses
    # abs(prior) so the sign reflects direction of improvement, not of the base.
    d = compute_delta(D("-50"), D("-100"), prior_has_data=True)
    assert d.state == "up"
    assert d.pct == D("50.0")
    assert d.label == "+50.0%"


def test_swing_from_profit_to_loss_reads_as_down():
    d = compute_delta(D("-50"), D("100"), prior_has_data=True)
    assert d.state == "down"
    assert d.pct == D("-150.0")
    assert d.label == "-150.0%"


def test_prior_none_overrides_prior_has_data_flag_defensively():
    # If a caller ever passes prior_has_data=True with a None value, don't crash.
    d = compute_delta(D("100"), None, prior_has_data=True)
    assert d.state == "new"
    assert d.pct is None


# --- mode="points": percentage-POINT deltas for ratios (e.g. margin) -------- #
def test_points_mode_is_subtraction_not_relative():
    # 40% -> 42% margin is +2.0pp, NOT +5.0% (relative would misread as points).
    d = compute_delta(D("0.42"), D("0.40"), prior_has_data=True, mode="points")
    assert d.state == "up"
    assert d.pct == D("2.0")
    assert d.label == "+2.0pp"


def test_points_mode_negative():
    d = compute_delta(D("0.38"), D("0.40"), prior_has_data=True, mode="points")
    assert d.state == "down"
    assert d.label == "-2.0pp"


def test_points_mode_zero_prior_is_a_real_change_not_new():
    # pp is a subtraction, so 0% -> 42% is a meaningful +42.0pp, not "+inf"/"new".
    d = compute_delta(D("0.42"), D("0"), prior_has_data=True, mode="points")
    assert d.state == "up"
    assert d.label == "+42.0pp"


def test_points_mode_missing_prior_still_new():
    d = compute_delta(D("0.42"), None, prior_has_data=False, mode="points")
    assert d.state == "new"
    assert d.label == "new"


def test_points_mode_both_zero_is_flat_pp_not_dash():
    # Deliberate pin: points is subtraction, so 0% -> 0% is a genuine 0.0pp
    # change (a real data point), NOT the undefined "—" we use in relative/
    # absolute where dividing by a zero base is meaningless.
    d = compute_delta(D("0"), D("0"), prior_has_data=True, mode="points")
    assert d.state == "flat"
    assert d.label == "0.0pp"


# --- mode="absolute": x-suffixed deltas for multipliers (e.g. ROAS) --------- #
def test_absolute_mode_uses_x_suffix_and_subtracts():
    d = compute_delta(D("3.3"), D("3.0"), prior_has_data=True, mode="absolute")
    assert d.state == "up"
    assert d.pct == D("0.3")
    assert d.label == "+0.3x"


def test_absolute_mode_zero_prior_is_new_not_a_jump_from_zero():
    # ROAS 0 = no ad-spend baseline; a "+3.2x" jump would be misleading.
    d = compute_delta(D("3.2"), D("0"), prior_has_data=True, mode="absolute")
    assert d.state == "new"
    assert d.label == "new"


# --------------------------------------------------------------------------- #
# sparkline_points — coordinate normalization (the div-by-zero traps)
# --------------------------------------------------------------------------- #
def test_empty_series_returns_empty_string():
    assert sparkline_points([]) == ""


def test_single_point_returns_empty_string():
    # A lone point can't draw a line; caller renders nothing / a flat dash.
    assert sparkline_points([D("5")]) == ""


def test_all_equal_series_does_not_divide_by_zero():
    pts = sparkline_points([D("5"), D("5"), D("5")], width=100, height=32, pad=2)
    coords = [p.split(",") for p in pts.split(" ")]
    ys = {c[1] for c in coords}
    # min == max must not blow up; all points share a single (mid) y.
    assert len(ys) == 1
    assert len(coords) == 3


def test_ascending_series_puts_max_at_top_min_at_bottom():
    # SVG y grows downward: the largest value should have the SMALLEST y.
    pts = sparkline_points([D("0"), D("10")], width=100, height=32, pad=2)
    (x0, y0), (x1, y1) = [tuple(map(float, p.split(","))) for p in pts.split(" ")]
    assert y0 > y1            # first (min) lower on screen than last (max)
    assert x0 < x1            # x increases left→right
    assert abs(y1 - 2) < 0.01     # max pinned to top pad
    assert abs(y0 - 30) < 0.01    # min pinned to bottom (height - pad)


# --------------------------------------------------------------------------- #
# trailing_months — pure calendar walk (year-boundary trap)
# --------------------------------------------------------------------------- #
def test_trailing_months_within_year():
    assert trailing_months(2026, 5, 3) == [(2026, 3), (2026, 4), (2026, 5)]


def test_trailing_months_crosses_year_boundary():
    assert trailing_months(2026, 2, 4) == [
        (2025, 11), (2025, 12), (2026, 1), (2026, 2),
    ]


def test_trailing_months_single():
    assert trailing_months(2026, 1, 1) == [(2026, 1)]


# --------------------------------------------------------------------------- #
# bar_chart — zero-baseline bar geometry (the negatives / div-by-zero traps)
# --------------------------------------------------------------------------- #
def test_bar_chart_empty_has_no_bars():
    assert bar_chart([]).bars == []


def test_bar_chart_all_positive_rise_from_bottom_baseline():
    c = bar_chart([D("10"), D("20")], width=100, height=40, pad=0)
    assert len(c.bars) == 2
    assert all(b.sign == "pos" for b in c.bars)
    assert abs(c.baseline - 40) < 0.01           # no negatives -> baseline at bottom
    assert c.bars[1].h > c.bars[0].h             # taller value -> taller bar
    for b in c.bars:                             # positive bars sit ON the baseline
        assert abs((b.y + b.h) - c.baseline) < 0.01


def test_bar_chart_all_negative_hang_from_top_baseline():
    c = bar_chart([D("-10"), D("-20")], width=100, height=40, pad=0)
    assert all(b.sign == "neg" for b in c.bars)
    assert abs(c.baseline - 0) < 0.01            # no positives -> baseline at top
    for b in c.bars:                             # negative bars hang FROM the baseline
        assert abs(b.y - c.baseline) < 0.01


def test_bar_chart_mixed_places_baseline_between():
    c = bar_chart([D("10"), D("-5")], width=100, height=40, pad=0)
    assert c.bars[0].sign == "pos"
    assert c.bars[1].sign == "neg"
    assert 0 < c.baseline < 40
    assert abs((c.bars[0].y + c.bars[0].h) - c.baseline) < 0.01   # pos above
    assert abs(c.bars[1].y - c.baseline) < 0.01                   # neg below


def test_bar_chart_all_zero_no_divide_by_zero():
    c = bar_chart([D("0"), D("0")], width=100, height=40, pad=0)
    assert len(c.bars) == 2
    assert all(b.h == 0 for b in c.bars)


def test_bar_chart_single_value():
    c = bar_chart([D("10")], width=100, height=40, pad=0)
    assert len(c.bars) == 1
    assert c.bars[0].sign == "pos"
