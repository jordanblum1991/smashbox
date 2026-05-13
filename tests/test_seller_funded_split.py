"""The exact-sum invariant is load-bearing for P&L reconciliation.

These tests assert it across rounding edge cases. If any of them fail, do NOT
'fix' them by relaxing the invariant — fix the split function so the sum holds.
"""
from decimal import Decimal

import pytest

from app.rules.seller_funded_split import split_seller_funded_discount


@pytest.mark.parametrize(
    "total, share",
    [
        ("100.00", "0.5"),
        ("100.01", "0.5"),    # odd cent — must not lose 1¢
        ("0.01", "0.5"),       # one-cent total
        ("0.03", "0.3333"),    # fractional share
        ("12345.67", "0.4275"),
        ("0.00", "0.5"),
        ("99999.99", "0.6667"),
        ("7.77", "0.7"),
    ],
)
def test_split_sums_back_exactly(total: str, share: str) -> None:
    s = split_seller_funded_discount(total, share)
    assert s.outlandish + s.smashbox == Decimal(total), (
        f"split drift: {s.outlandish} + {s.smashbox} != {total}"
    )


def test_share_zero_gives_all_to_smashbox() -> None:
    s = split_seller_funded_discount("123.45", "0")
    assert s.outlandish == Decimal("0.00")
    assert s.smashbox == Decimal("123.45")


def test_share_one_gives_all_to_outlandish() -> None:
    s = split_seller_funded_discount("123.45", "1")
    assert s.outlandish == Decimal("123.45")
    assert s.smashbox == Decimal("0.00")


def test_share_out_of_range_raises() -> None:
    with pytest.raises(ValueError):
        split_seller_funded_discount("100.00", "1.5")
    with pytest.raises(ValueError):
        split_seller_funded_discount("100.00", "-0.1")


def test_accepts_decimal_float_str_int() -> None:
    a = split_seller_funded_discount(Decimal("10.00"), Decimal("0.5"))
    b = split_seller_funded_discount(10.0, 0.5)
    c = split_seller_funded_discount("10.00", "0.5")
    d = split_seller_funded_discount(10, "0.5")
    assert a.outlandish == b.outlandish == c.outlandish == d.outlandish == Decimal("5.00")


def test_invariant_is_enforced_at_construction() -> None:
    """The DiscountSplit dataclass itself rejects a bad sum — belt and suspenders."""
    from app.rules.seller_funded_split import DiscountSplit  # noqa: PLC0415
    with pytest.raises(AssertionError):
        DiscountSplit(total=Decimal("10.00"), outlandish=Decimal("4.00"), smashbox=Decimal("5.00"))
