"""Split TikTok seller-funded discounts between Outlandish and Smashbox.

Business rule (confirmed 2026-05-13):

    Outlandish = MIN(seller_funded_total, eligible_base × cap_pct)
    Smashbox   = seller_funded_total - Outlandish

Where:
  - seller_funded_total is the "SKU Seller Discount" total for the order
    (the only piece TikTok marks as seller-funded; "SKU Platform Discount"
    is TikTok-funded and not split).
  - eligible_base is the order's gross price basis used for discount %
    calculations — i.e. the sum of "SKU Subtotal Before Discount" across
    the order's lines (== Order.gross_sales).
  - cap_pct defaults to 10% (settings.outlandish_cap_pct).

INVARIANT (load-bearing): Outlandish + Smashbox == seller_funded_total, exactly.
No rounding drift — ever. P&L reconciliation depends on this. Outlandish is
computed and quantized; Smashbox is the residual, so the sum is exact by
construction.
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
    eligible_base: Decimal | float | str | int = 0,
    cap_pct: Decimal | float | str | None = None,
) -> DiscountSplit:
    """Cap-then-residual split. See module docstring."""
    total_d = _to_decimal(total).quantize(CENTS, rounding=ROUND_HALF_EVEN)
    base_d = _to_decimal(eligible_base).quantize(CENTS, rounding=ROUND_HALF_EVEN)
    pct = _to_decimal(settings.outlandish_cap_pct if cap_pct is None else cap_pct)

    if not (Decimal("0") <= pct <= Decimal("1")):
        raise ValueError(f"cap_pct must be in [0, 1], got {pct}")

    cap = (base_d * pct).quantize(CENTS, rounding=ROUND_HALF_EVEN)
    outlandish = min(total_d, cap)
    # Guard against pathological negative inputs leaking through.
    if outlandish < Decimal("0"):
        outlandish = Decimal("0.00")
    smashbox = total_d - outlandish

    return DiscountSplit(total=total_d, outlandish=outlandish, smashbox=smashbox)


def _to_decimal(v: Decimal | float | str | int) -> Decimal:
    if isinstance(v, Decimal):
        return v
    return Decimal(str(v))
