"""Split TikTok seller-funded discounts between Outlandish and Smashbox.

INVARIANT (load-bearing): outlandish + smashbox == total, exactly. No rounding
drift — ever. P&L reconciliation depends on this. The Outlandish share is
computed and quantized; the Smashbox share is whatever's left.

The split ratio comes from settings.seller_funded_outlandish_share by default,
but callers can pass a per-SKU or per-order override.
"""
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from app.config import settings

CENTS = Decimal("0.01")


@dataclass(frozen=True)
class DiscountSplit:
    total: Decimal
    outlandish: Decimal
    smashbox: Decimal

    def __post_init__(self) -> None:
        if self.outlandish + self.smashbox != self.total:
            raise AssertionError(
                f"split invariant violated: {self.outlandish} + {self.smashbox} != {self.total}"
            )


def split_seller_funded_discount(
    total: Decimal | float | str | int,
    outlandish_share: Decimal | float | str | None = None,
) -> DiscountSplit:
    """Split `total` so the two parts add back to it exactly.

    `outlandish_share` is a fraction in [0, 1]. The Outlandish portion is
    rounded to cents using banker's rounding; the Smashbox portion absorbs any
    residual so the sum is exact.
    """
    total_d = _to_decimal(total).quantize(CENTS, rounding=ROUND_HALF_EVEN)
    share = _to_decimal(
        settings.seller_funded_outlandish_share if outlandish_share is None else outlandish_share
    )

    if not (Decimal("0") <= share <= Decimal("1")):
        raise ValueError(f"outlandish_share must be in [0, 1], got {share}")

    outlandish = (total_d * share).quantize(CENTS, rounding=ROUND_HALF_EVEN)
    smashbox = total_d - outlandish  # residual — guarantees exact sum

    return DiscountSplit(total=total_d, outlandish=outlandish, smashbox=smashbox)


def _to_decimal(v: Decimal | float | str | int) -> Decimal:
    if isinstance(v, Decimal):
        return v
    return Decimal(str(v))
