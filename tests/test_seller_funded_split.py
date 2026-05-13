"""Seller-funded discount split: cap-then-residual rule.

  Outlandish = MIN(total, eligible_base * cap_pct)
  Smashbox   = total - Outlandish

The exact-sum invariant is load-bearing for P&L reconciliation. These tests
assert it across rounding edge cases AND the cap behavior. Do NOT 'fix' a
failing test by relaxing the invariant — fix the split function.
"""
from decimal import Decimal

import pytest

from app.rules.seller_funded_split import (
    DiscountSplit,
    split_seller_funded_discount,
)


def test_canonical_example_25_on_100() -> None:
    """The example pinned by the user: base $100, discount $25 -> $10 / $15."""
    s = split_seller_funded_discount("25.00", eligible_base="100.00", cap_pct="0.10")
    assert s.outlandish == Decimal("10.00")
    assert s.smashbox == Decimal("15.00")
    assert s.total == Decimal("25.00")


def test_discount_below_cap_goes_entirely_to_outlandish() -> None:
    """Base $50, 10% cap = $5. Discount $3 < cap, so Outlandish takes all $3."""
    s = split_seller_funded_discount("3.00", eligible_base="50.00", cap_pct="0.10")
    assert s.outlandish == Decimal("3.00")
    assert s.smashbox == Decimal("0.00")


def test_discount_at_exact_cap() -> None:
    s = split_seller_funded_discount("5.00", eligible_base="50.00", cap_pct="0.10")
    assert s.outlandish == Decimal("5.00")
    assert s.smashbox == Decimal("0.00")


def test_discount_above_cap_residual_to_smashbox() -> None:
    s = split_seller_funded_discount("8.00", eligible_base="50.00", cap_pct="0.10")
    assert s.outlandish == Decimal("5.00")
    assert s.smashbox == Decimal("3.00")


@pytest.mark.parametrize(
    "total, base, pct",
    [
        ("100.00", "1000.00", "0.10"),
        ("100.01", "1000.10", "0.10"),    # odd-cent base
        ("0.01", "0.10", "0.10"),          # one-cent
        ("0.03", "0.30", "0.3333"),        # fractional cap
        ("12345.67", "98765.43", "0.10"),
        ("0.00", "100.00", "0.10"),        # zero discount
        ("99999.99", "10000.00", "0.10"),  # discount way above cap
        ("7.77", "77.77", "0.10"),
    ],
)
def test_split_sums_back_exactly(total: str, base: str, pct: str) -> None:
    s = split_seller_funded_discount(total, eligible_base=base, cap_pct=pct)
    assert s.outlandish + s.smashbox == Decimal(total), (
        f"split drift: {s.outlandish} + {s.smashbox} != {total}"
    )


def test_zero_base_means_outlandish_zero() -> None:
    """No eligible base -> cap = $0 -> Outlandish = $0 -> Smashbox owns the discount."""
    s = split_seller_funded_discount("12.50", eligible_base="0", cap_pct="0.10")
    assert s.outlandish == Decimal("0.00")
    assert s.smashbox == Decimal("12.50")


def test_zero_discount_returns_zeros() -> None:
    s = split_seller_funded_discount("0", eligible_base="100.00", cap_pct="0.10")
    assert s.outlandish == Decimal("0.00")
    assert s.smashbox == Decimal("0.00")


def test_uses_settings_cap_when_pct_omitted() -> None:
    """Default cap comes from settings.outlandish_cap_pct (currently 0.10)."""
    s = split_seller_funded_discount("25.00", eligible_base="100.00")
    assert s.outlandish == Decimal("10.00")
    assert s.smashbox == Decimal("15.00")


def test_cap_pct_out_of_range_raises() -> None:
    with pytest.raises(ValueError):
        split_seller_funded_discount("100.00", eligible_base="1000.00", cap_pct="1.5")
    with pytest.raises(ValueError):
        split_seller_funded_discount("100.00", eligible_base="1000.00", cap_pct="-0.1")


def test_accepts_decimal_float_str_int() -> None:
    a = split_seller_funded_discount(Decimal("25"), Decimal("100"), Decimal("0.10"))
    b = split_seller_funded_discount(25.0, 100.0, 0.10)
    c = split_seller_funded_discount("25.00", "100.00", "0.10")
    d = split_seller_funded_discount(25, 100, "0.10")
    assert a.outlandish == b.outlandish == c.outlandish == d.outlandish == Decimal("10.00")


def test_invariant_enforced_at_construction() -> None:
    """The DiscountSplit dataclass itself rejects a bad sum — belt and suspenders."""
    with pytest.raises(AssertionError):
        DiscountSplit(total=Decimal("10.00"), outlandish=Decimal("4.00"), smashbox=Decimal("5.00"))
