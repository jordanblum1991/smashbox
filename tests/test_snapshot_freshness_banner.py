"""Tests for the planner snapshot-freshness banner state.

The banner is keyed off `DemandPlanningView.snapshot_freshness_state`,
which buckets `snapshot_age_days` into four tiers. This test pins the
day thresholds so a refactor can't silently shift them.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.reports.demand_planning import DemandPlanningView


def _mk(age: int | None) -> DemandPlanningView:
    """Construct a minimal view with a given snapshot_age_days. All other
    fields are placeholder defaults — they don't affect the property."""
    return DemandPlanningView(
        rows=[],
        as_of=date(2026, 5, 20),
        latest_snapshot_at=None,
        snapshot_is_stale=False,
        snapshot_age_days=age,
        safety_stock_pct=Decimal("0.10"),
        cover_days=45,
        overstocked_days=180,
        investment_total=Decimal("0"),
        investment_30d=Decimal("0"),
        investment_60d=Decimal("0"),
        investment_90d=Decimal("0"),
        investment_180d=Decimal("0"),
    )


@pytest.mark.parametrize("age,expected", [
    # Fresh: 0–7 days inclusive.
    (0, "fresh"),
    (1, "fresh"),
    (7, "fresh"),
    # Aging: 8–14 days inclusive.
    (8, "aging"),
    (10, "aging"),
    (14, "aging"),
    # Stale: 15+ days.
    (15, "stale"),
    (30, "stale"),
    (365, "stale"),
])
def test_freshness_state_thresholds(age, expected):
    assert _mk(age).snapshot_freshness_state == expected


def test_freshness_state_missing_when_no_snapshot_age():
    """`snapshot_age_days = None` means no snapshot has ever been uploaded."""
    assert _mk(None).snapshot_freshness_state == "missing"


def test_freshness_state_boundary_at_8_days():
    """7 days is the last 'fresh' day; 8 is the first 'aging' day. Spec is
    inclusive on the lower bound of each tier."""
    assert _mk(7).snapshot_freshness_state == "fresh"
    assert _mk(8).snapshot_freshness_state == "aging"


def test_freshness_state_boundary_at_15_days():
    """14 days is the last 'aging' day; 15 is the first 'stale' day."""
    assert _mk(14).snapshot_freshness_state == "aging"
    assert _mk(15).snapshot_freshness_state == "stale"
