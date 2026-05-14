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
    violates_policy_cap,
)


def test_canonical_post_tiktok_example() -> None:
    """User's canonical worked example, 2026-05-13.

    Gross product sales      = $100
    TikTok-funded discount   = $20    -> post-TikTok price = $80
    Total seller-funded disc = $24
    Outlandish max (10%)     = $8     -> Outlandish = MIN($24, $8) = $8
    Smashbox                 = $24 - $8 = $16
    """
    post_tiktok = Decimal("100") - Decimal("20")
    s = split_seller_funded_discount("24.00", eligible_base=post_tiktok, cap_pct="0.10")
    assert s.outlandish == Decimal("8.00")
    assert s.smashbox == Decimal("16.00")
    assert s.total == Decimal("24.00")
    assert s.outlandish + s.smashbox == Decimal("24.00")


def test_split_25_on_100_no_tiktok_discount() -> None:
    """When there's no TikTok-funded discount, the base equals gross."""
    s = split_seller_funded_discount("25.00", eligible_base="100.00", cap_pct="0.10")
    assert s.outlandish == Decimal("10.00")
    assert s.smashbox == Decimal("15.00")


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


# ---------------------------------------------------------------------------
# Policy cap: total seller-funded should NEVER exceed 30% of eligible base.

def test_policy_at_or_below_cap_is_not_a_violation() -> None:
    # 30% exactly is OK.
    assert not violates_policy_cap("30.00", eligible_base="100.00", policy_cap_pct="0.30")
    # Below cap.
    assert not violates_policy_cap("25.00", eligible_base="100.00", policy_cap_pct="0.30")
    # No discount.
    assert not violates_policy_cap("0", eligible_base="100.00", policy_cap_pct="0.30")


def test_policy_above_cap_is_a_violation() -> None:
    assert violates_policy_cap("30.01", eligible_base="100.00", policy_cap_pct="0.30")
    assert violates_policy_cap("26.00", eligible_base="81.00", policy_cap_pct="0.30")  # the real one


def test_policy_base_is_msrp_not_post_tiktok() -> None:
    """Real example: SKU C2NP11 — 25.9% of MSRP (OK), 41.2% of post-TikTok (would trip).

    The importer passes MSRP (gross) as the policy base, so this line is
    NOT a violation under our policy.
    """
    # Should NOT trip when checked against MSRP $27.00:
    assert not violates_policy_cap("7.00", eligible_base="27.00", policy_cap_pct="0.30")
    # Would trip if we (wrongly) checked against post-TikTok $17.00:
    assert violates_policy_cap("7.00", eligible_base="17.00", policy_cap_pct="0.30")


def test_policy_violation_does_not_break_split_invariant() -> None:
    """Even when policy is breached, Outlandish + Smashbox == total exactly."""
    s = split_seller_funded_discount("26.00", eligible_base="81.00", cap_pct="0.10")
    assert s.outlandish == Decimal("8.10")
    assert s.smashbox == Decimal("17.90")
    assert s.outlandish + s.smashbox == Decimal("26.00")


def test_policy_zero_base_with_discount_is_a_violation() -> None:
    """A non-zero discount on a $0 order can't possibly be within policy."""
    assert violates_policy_cap("5.00", eligible_base="0", policy_cap_pct="0.30")
