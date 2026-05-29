"""Split TikTok seller-funded discounts between Outlandish and Smashbox.

Business rule (three-band, confirmed 2026-05-29):

    cap10        = cap_pct × eligible_base         (post-TikTok price)
    cap30        = policy_cap_pct × policy_base    (gross / MSRP)
    Outlandish   = MIN(total, cap10) + MAX(0, total − cap30)
    Smashbox     = total − Outlandish
                 (== MAX(0, MIN(total, cap30) − cap10))

Where:
  - total is "SKU Seller Discount" per line.
  - eligible_base is the line's post-TikTok price (gross − platform_discount);
    cap_pct (default 10%) operates on this.
  - policy_base is the line's gross / MSRP; policy_cap_pct (default 30%)
    operates on this. The 30% ceiling intentionally uses the same base as
    `violates_policy_cap`, so any seller discount above 30% of MSRP — the
    over-policy excess — is funded by Outlandish, not Smashbox.
  - Bands: Outlandish funds the first 10pp AND anything above the 30%
    ceiling. Smashbox funds only the 10–30 band (the in-policy excess).

INVARIANT (load-bearing): Outlandish + Smashbox == total, exactly.
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
    *,
    policy_base: Decimal | float | str | int | None = None,
    policy_cap_pct: Decimal | float | str | None = None,
) -> DiscountSplit:
    """Three-band cap-then-residual split. See module docstring."""
    total_d = _to_decimal(total).quantize(CENTS, rounding=ROUND_HALF_EVEN)
    base_d = _to_decimal(eligible_base).quantize(CENTS, rounding=ROUND_HALF_EVEN)
    pol_base_d = (
        base_d if policy_base is None
        else _to_decimal(policy_base).quantize(CENTS, rounding=ROUND_HALF_EVEN)
    )
    pct = _to_decimal(settings.outlandish_cap_pct if cap_pct is None else cap_pct)
    pol_pct = _to_decimal(
        settings.seller_funded_policy_cap_pct if policy_cap_pct is None else policy_cap_pct
    )

    if not (Decimal("0") <= pct <= Decimal("1")):
        raise ValueError(f"cap_pct must be in [0, 1], got {pct}")
    if not (Decimal("0") <= pol_pct <= Decimal("1")):
        raise ValueError(f"policy_cap_pct must be in [0, 1], got {pol_pct}")
    if pol_pct < pct:
        raise ValueError(
            f"policy_cap_pct ({pol_pct}) must be >= cap_pct ({pct})"
        )

    cap10 = (base_d * pct).quantize(CENTS, rounding=ROUND_HALF_EVEN)
    cap30 = (pol_base_d * pol_pct).quantize(CENTS, rounding=ROUND_HALF_EVEN)
    # Defensive: with post-TikTok floor and gross ceiling, post_tt <= gross so
    # cap10 <= cap30 always. Clamp anyway against pathological inputs that
    # would invert the bands.
    if cap10 > cap30:
        cap10 = cap30

    over_policy = max(Decimal("0.00"), total_d - cap30)
    outlandish = min(total_d, cap10) + over_policy
    if outlandish < Decimal("0"):
        outlandish = Decimal("0.00")
    if outlandish > total_d:
        outlandish = total_d
    # Smashbox is the residual so the exact-sum invariant holds by construction.
    smashbox = total_d - outlandish

    return DiscountSplit(total=total_d, outlandish=outlandish, smashbox=smashbox)


def violates_policy_cap(
    total: Decimal | float | str | int,
    eligible_base: Decimal | float | str | int,
    policy_cap_pct: Decimal | float | str | None = None,
) -> bool:
    """True iff the total seller-funded discount exceeds the policy ceiling.

    NOTE: the policy ceiling uses MSRP / gross as `eligible_base` (conventional
    discount-percentage language: "no SKU goes over 30% off retail"), which is
    DIFFERENT from the base used by `split_seller_funded_discount` (post-TikTok
    price). Don't confuse the two — the importer passes them separately.

    Under our policy this should NEVER trip. When it does, callers should still
    import the line (Smashbox absorbs the excess so Outlandish + Smashbox == total)
    but flag it via OrderLine.discount_policy_violation.
    """
    total_d = _to_decimal(total)
    base_d = _to_decimal(eligible_base)
    cap_pct = _to_decimal(
        settings.seller_funded_policy_cap_pct if policy_cap_pct is None else policy_cap_pct
    )
    if base_d <= 0:
        return total_d > Decimal("0")  # any discount on a $0-base order is suspect
    return total_d > base_d * cap_pct


def _to_decimal(v: Decimal | float | str | int) -> Decimal:
    if isinstance(v, Decimal):
        return v
    return Decimal(str(v))
