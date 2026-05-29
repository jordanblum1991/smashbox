"""Seller-funded discount split: three-band cap-then-residual rule.

  cap10        = cap_pct × eligible_base         (post-TikTok price)
  cap30        = policy_cap_pct × policy_base    (gross / MSRP)
  Outlandish   = MIN(total, cap10) + MAX(0, total − cap30)
  Smashbox     = total − Outlandish

Outlandish absorbs the FIRST 10pp AND anything ABOVE the 30% policy ceiling.
Smashbox owns only the 10–30 band. The exact-sum invariant is load-bearing
for P&L reconciliation. Do NOT 'fix' a failing test by relaxing the
invariant — fix the split function.
"""
from decimal import Decimal

import pytest

from app.rules.seller_funded_split import (
    DiscountSplit,
    split_seller_funded_discount,
    violates_policy_cap,
)


# ---------------------------------------------------------------------------
# Canonical worked examples for the three-band rule
# ---------------------------------------------------------------------------

def test_canonical_35pct_on_100_yields_15_20() -> None:
    """User's confirmation example, 2026-05-29.

    Line: gross=$100, no TikTok promo (post_tiktok=$100), seller_disc=$35.
    cap10 = $10, cap30 = $30, over_policy = $5.
    Outlandish = $10 + $5 = $15. Smashbox = $35 - $15 = $20.

    NOT Outlandish $10 / Smashbox $25 (which would be the old uncapped-residual
    behavior). The over-policy $5 belongs to Outlandish.
    """
    s = split_seller_funded_discount(
        "35.00",
        eligible_base="100.00",
        cap_pct="0.10",
        policy_base="100.00",
        policy_cap_pct="0.30",
    )
    assert s.outlandish == Decimal("15.00")
    assert s.smashbox == Decimal("20.00")
    assert s.total == Decimal("35.00")
    assert s.outlandish + s.smashbox == s.total


def test_canonical_post_tiktok_example_24_on_80_gross_100() -> None:
    """CLAUDE.md worked example, with importer-style mixed bases.

    Gross product sales       = $100
    TikTok-funded discount    = $20    -> post-TikTok price = $80
    Total seller-funded disc  = $24

    cap10 = 10% × $80 = $8           (post-TikTok floor base)
    cap30 = 30% × $100 = $30         (gross ceiling base)
    over_policy = MAX(0, $24 − $30) = $0
    Outlandish = MIN($24, $8) + $0 = $8
    Smashbox   = $24 − $8 = $16
    """
    s = split_seller_funded_discount(
        "24.00",
        eligible_base="80.00",
        cap_pct="0.10",
        policy_base="100.00",
        policy_cap_pct="0.30",
    )
    assert s.outlandish == Decimal("8.00")
    assert s.smashbox == Decimal("16.00")
    assert s.outlandish + s.smashbox == Decimal("24.00")


# ---------------------------------------------------------------------------
# Band coverage — below cap10, exactly cap10, in 10–30 band, exactly cap30,
# above cap30 (over-policy)
# ---------------------------------------------------------------------------

def test_discount_below_cap10_goes_entirely_to_outlandish() -> None:
    """Base $50, cap10 = $5. Discount $3 < cap10, so Outlandish takes all $3."""
    s = split_seller_funded_discount(
        "3.00", eligible_base="50.00", cap_pct="0.10",
        policy_base="50.00", policy_cap_pct="0.30",
    )
    assert s.outlandish == Decimal("3.00")
    assert s.smashbox == Decimal("0.00")


def test_discount_at_exact_cap10() -> None:
    """At cap10 exactly: all Outlandish, Smashbox $0 — the 10–30 band stays empty."""
    s = split_seller_funded_discount(
        "5.00", eligible_base="50.00", cap_pct="0.10",
        policy_base="50.00", policy_cap_pct="0.30",
    )
    assert s.outlandish == Decimal("5.00")
    assert s.smashbox == Decimal("0.00")


def test_discount_in_band_goes_to_smashbox_residual() -> None:
    """Base $50, cap10=$5, cap30=$15. Discount $8 sits in the 10–30 band.

    Outlandish absorbs the first $5; Smashbox absorbs the $3 above cap10.
    No over-policy excess. Identical to the legacy rule on this case — the
    new rule only diverges above cap30.
    """
    s = split_seller_funded_discount(
        "8.00", eligible_base="50.00", cap_pct="0.10",
        policy_base="50.00", policy_cap_pct="0.30",
    )
    assert s.outlandish == Decimal("5.00")
    assert s.smashbox == Decimal("3.00")


def test_discount_at_exact_cap30() -> None:
    """At cap30 exactly: Outlandish = cap10, Smashbox = cap30 − cap10."""
    s = split_seller_funded_discount(
        "15.00", eligible_base="50.00", cap_pct="0.10",
        policy_base="50.00", policy_cap_pct="0.30",
    )
    assert s.outlandish == Decimal("5.00")
    assert s.smashbox == Decimal("10.00")


def test_discount_above_cap30_excess_goes_to_outlandish() -> None:
    """Base $50, cap10=$5, cap30=$15. Discount $20.

    Outlandish absorbs cap10 ($5) PLUS the over-policy $5. Smashbox is
    capped at the band width ($15 − $5 = $10).
    """
    s = split_seller_funded_discount(
        "20.00", eligible_base="50.00", cap_pct="0.10",
        policy_base="50.00", policy_cap_pct="0.30",
    )
    assert s.outlandish == Decimal("10.00")   # $5 floor + $5 over-policy
    assert s.smashbox == Decimal("10.00")     # band width


# ---------------------------------------------------------------------------
# Mixed-base behavior (what the importer actually does)
# ---------------------------------------------------------------------------

def test_mixed_bases_cap10_on_post_tiktok_cap30_on_gross() -> None:
    """Real line: gross=$100, plat_disc=$20, post_tt=$80, seller=$30.

    cap10 = 10% × $80  = $8     (post-TikTok)
    cap30 = 30% × $100 = $30    (gross)
    over_policy = MAX(0, $30 − $30) = $0
    Outlandish = MIN($30, $8) + $0 = $8
    Smashbox   = $30 − $8 = $22

    The same line under same-base semantics (both 10% and 30% on post-TikTok)
    would have given cap30 = 0.30 × $80 = $24, over_policy = $6, Outlandish
    = $14, Smashbox = $16. Confirms the mixed-base choice MATTERS.
    """
    s = split_seller_funded_discount(
        "30.00",
        eligible_base="80.00",
        cap_pct="0.10",
        policy_base="100.00",
        policy_cap_pct="0.30",
    )
    assert s.outlandish == Decimal("8.00")
    assert s.smashbox == Decimal("22.00")


def test_policy_base_defaults_to_eligible_base_when_omitted() -> None:
    """If the caller doesn't pass policy_base, both bands use eligible_base.
    This is the legacy single-base mode and remains supported.

    On $100 / 25% discount with single base: cap10=$10, cap30=$30. seller=$25
    in-band → Outlandish=$10, Smashbox=$15.
    """
    s = split_seller_funded_discount("25.00", eligible_base="100.00", cap_pct="0.10")
    assert s.outlandish == Decimal("10.00")
    assert s.smashbox == Decimal("15.00")


# ---------------------------------------------------------------------------
# Invariant tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "total, base, pct",
    [
        ("100.00", "1000.00", "0.10"),
        ("100.01", "1000.10", "0.10"),    # odd-cent base
        ("0.01", "0.10", "0.10"),          # one-cent
        ("0.03", "0.30", "0.1333"),        # fractional cap (must be <= 30% policy)
        ("12345.67", "98765.43", "0.10"),
        ("0.00", "100.00", "0.10"),        # zero discount
        ("99999.99", "10000.00", "0.10"),  # discount way above cap30
        ("7.77", "77.77", "0.10"),
        ("3000.00", "10000.00", "0.10"),   # exactly at default cap30 (single-base)
        ("3500.00", "10000.00", "0.10"),   # 35% — over policy
    ],
)
def test_split_sums_back_exactly(total: str, base: str, pct: str) -> None:
    s = split_seller_funded_discount(total, eligible_base=base, cap_pct=pct)
    assert s.outlandish + s.smashbox == Decimal(total), (
        f"split drift: {s.outlandish} + {s.smashbox} != {total}"
    )


def test_zero_base_with_discount_all_goes_to_outlandish() -> None:
    """eligible_base=$0 → cap10=$0; policy_base defaults to $0 → cap30=$0.

    Under the three-band rule, total > cap30 means the entire discount is
    over-policy and lands on Outlandish. (Old behavior: Smashbox owned it.)
    A non-zero discount on a $0-base line is a data smell either way — the
    `violates_policy_cap` function flags it for finance to look at.
    """
    s = split_seller_funded_discount("12.50", eligible_base="0", cap_pct="0.10")
    assert s.outlandish == Decimal("12.50")
    assert s.smashbox == Decimal("0.00")


def test_zero_discount_returns_zeros() -> None:
    s = split_seller_funded_discount("0", eligible_base="100.00", cap_pct="0.10")
    assert s.outlandish == Decimal("0.00")
    assert s.smashbox == Decimal("0.00")


def test_uses_settings_caps_when_pcts_omitted() -> None:
    """Defaults from settings: cap_pct=0.10, policy_cap_pct=0.30."""
    s = split_seller_funded_discount("25.00", eligible_base="100.00")
    assert s.outlandish == Decimal("10.00")
    assert s.smashbox == Decimal("15.00")


def test_cap_pct_out_of_range_raises() -> None:
    with pytest.raises(ValueError):
        split_seller_funded_discount("100.00", eligible_base="1000.00", cap_pct="1.5")
    with pytest.raises(ValueError):
        split_seller_funded_discount("100.00", eligible_base="1000.00", cap_pct="-0.1")


def test_policy_cap_pct_out_of_range_raises() -> None:
    with pytest.raises(ValueError):
        split_seller_funded_discount(
            "100.00", eligible_base="1000.00", policy_cap_pct="1.5"
        )
    with pytest.raises(ValueError):
        split_seller_funded_discount(
            "100.00", eligible_base="1000.00", policy_cap_pct="-0.1"
        )


def test_policy_cap_pct_below_cap_pct_raises() -> None:
    """policy_cap_pct must be >= cap_pct, else the bands invert."""
    with pytest.raises(ValueError, match="must be >="):
        split_seller_funded_discount(
            "10.00",
            eligible_base="100.00",
            cap_pct="0.30",
            policy_cap_pct="0.10",
        )


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
# Policy cap flag (violates_policy_cap is unchanged by the three-band split)
# ---------------------------------------------------------------------------

def test_policy_at_or_below_cap_is_not_a_violation() -> None:
    # 30% exactly is OK.
    assert not violates_policy_cap("30.00", eligible_base="100.00", policy_cap_pct="0.30")
    # Below cap.
    assert not violates_policy_cap("25.00", eligible_base="100.00", policy_cap_pct="0.30")
    # No discount.
    assert not violates_policy_cap("0", eligible_base="100.00", policy_cap_pct="0.30")


def test_policy_above_cap_is_a_violation() -> None:
    assert violates_policy_cap("30.01", eligible_base="100.00", policy_cap_pct="0.30")
    assert violates_policy_cap("26.00", eligible_base="81.00", policy_cap_pct="0.30")


def test_policy_base_is_msrp_not_post_tiktok() -> None:
    """Real example: SKU C2NP11 — 25.9% of MSRP (OK), 41.2% of post-TikTok (would trip).

    The importer passes MSRP (gross) as the policy base, so this line is
    NOT a violation under our policy.
    """
    # Should NOT trip when checked against MSRP $27.00:
    assert not violates_policy_cap("7.00", eligible_base="27.00", policy_cap_pct="0.30")
    # Would trip if we (wrongly) checked against post-TikTok $17.00:
    assert violates_policy_cap("7.00", eligible_base="17.00", policy_cap_pct="0.30")


def test_policy_violation_splits_excess_to_outlandish() -> None:
    """A policy-violating line under the three-band rule:

    base = $81, cap_pct = 10%, policy_cap_pct = 30%, total = $26.
    cap10 = $8.10, cap30 = $24.30, over_policy = $26 − $24.30 = $1.70.
    Outlandish = $8.10 + $1.70 = $9.80.
    Smashbox   = $26 − $9.80 = $16.20.

    (Under the old rule this was Outlandish $8.10 / Smashbox $17.90 — the
    over-policy $1.70 has shifted from Smashbox to Outlandish.)
    """
    s = split_seller_funded_discount("26.00", eligible_base="81.00", cap_pct="0.10")
    assert s.outlandish == Decimal("9.80")
    assert s.smashbox == Decimal("16.20")
    assert s.outlandish + s.smashbox == Decimal("26.00")


def test_policy_zero_base_with_discount_is_a_violation() -> None:
    """A non-zero discount on a $0 order can't possibly be within policy."""
    assert violates_policy_cap("5.00", eligible_base="0", policy_cap_pct="0.30")
