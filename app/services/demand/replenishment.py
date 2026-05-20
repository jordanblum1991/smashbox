"""Replenishment math — for each SKU, given current on-hand + velocity +
procurement attributes, decide whether to reorder and how many to buy.

Pure functions. Inputs are explicit (no global state) so unit tests can
construct any scenario.

Status classification:

    out_of_stock   on_hand <= 0
    at_risk        stockout will happen BEFORE a fresh PO can arrive
    reorder_now    on_hand below reorder point (still has runway, but a
                   PO needs to go in today to land before stockout)
    healthy        on_hand above reorder point but below overstock line
    overstocked    days_of_supply > overstocked_threshold_days
    discontinued   is_reorderable == False — won't be reordered regardless

Order quantity math:

    target_qty  = velocity × (lead_time + cover_days) − on_hand − expected_receipts
    target_qty  = max(target_qty, MOQ) if MOQ is set
    target_qty  = round_up_to(case_pack) if case_pack is set
    target_qty  = max(target_qty, 0)   ← never negative

`expected_receipts` is an optional buyer-supplied number per SKU representing
in-transit or known-incoming inventory (the planner UI lets you type it in).
Defaults to 0.
"""
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum
from math import ceil


class ReplenishmentStatus(str, Enum):
    OUT_OF_STOCK = "out_of_stock"
    AT_RISK = "at_risk"
    REORDER_NOW = "reorder_now"
    HEALTHY = "healthy"
    OVERSTOCKED = "overstocked"
    DISCONTINUED = "discontinued"
    NO_VELOCITY = "no_velocity"   # never sold in the window — can't compute


@dataclass(frozen=True)
class ReplenishmentInputs:
    """Everything the math needs. Caller assembles this from Sku + InventorySnapshot
    + velocity + procurement-defaults; the math doesn't query the DB."""
    sku_code: str | None          # human-readable (e.g. SBX-XXX); for display
    component_sku: str            # the actual key used in matching
    name: str | None              # product name for display

    on_hand: int
    expected_receipts: int        # in-transit / buyer override (default 0)

    # 60-day baseline. `daily_velocity` is the ROBUST (spike-dampened) rate —
    # drives reorder point, suggested qty, and investment math (conservative
    # buying). `daily_velocity_raw` is the unclipped mean and drives
    # days-of-supply, stockout date, and at-risk/out-of-stock flags so a
    # viral spike still surfaces as risk. Falls back to `daily_velocity`
    # when omitted, so pre-dampening callers and unit tests still work.
    daily_velocity: Decimal
    daily_velocity_14d: Decimal   # short-term comparison
    daily_velocity_raw: Decimal | None = None

    lead_time_days: int = 14
    safety_stock_pct: Decimal = Decimal("0.10")
    cover_days: int = 45
    overstocked_threshold_days: int = 180

    moq: int = 0                  # minimum order qty (0 = no minimum)
    case_pack: int = 0            # round up to this multiple (0/1 = no rounding)
    is_reorderable: bool = True
    unit_cogs: Decimal = Decimal("0")  # for investment $$ math


@dataclass
class ReplenishmentResult:
    """What the planner page renders per SKU."""
    sku_code: str | None
    component_sku: str
    name: str | None

    on_hand: int
    expected_receipts: int
    available: int                # on_hand + expected_receipts

    daily_velocity: Decimal
    daily_velocity_14d: Decimal
    trend_ratio: Decimal          # 14d / 60d, 1.0 if no signal

    days_of_supply: Decimal | None
    stockout_date: date | None

    lead_time_days: int
    reorder_point: int
    suggested_order_qty: int
    investment: Decimal

    status: ReplenishmentStatus


def compute_one(inp: ReplenishmentInputs, *, as_of: date) -> ReplenishmentResult:
    """Single-SKU replenishment calculation."""
    available = inp.on_hand + inp.expected_receipts
    v = inp.daily_velocity                              # ROBUST — drives buying math
    v_raw = inp.daily_velocity_raw if inp.daily_velocity_raw is not None else v

    # Status: no-velocity SKUs can't drive math (would divide by zero). Use
    # the raw rate to detect "we have no signal" — if there were ANY recent
    # sales we have a signal, even if the robust mean got dampened to zero
    # (only theoretically possible when raw is also zero).
    if v_raw <= 0:
        status = ReplenishmentStatus.DISCONTINUED if not inp.is_reorderable \
            else ReplenishmentStatus.NO_VELOCITY
        return ReplenishmentResult(
            sku_code=inp.sku_code, component_sku=inp.component_sku, name=inp.name,
            on_hand=inp.on_hand, expected_receipts=inp.expected_receipts,
            available=available,
            daily_velocity=v, daily_velocity_14d=inp.daily_velocity_14d,
            trend_ratio=Decimal("1"),
            days_of_supply=None, stockout_date=None,
            lead_time_days=inp.lead_time_days,
            reorder_point=0, suggested_order_qty=0,
            investment=Decimal("0"),
            status=status,
        )

    # Trend ratio: RAW vs RAW. Surfaces real demand shape — clipping would
    # muddy the very signal the trend is meant to reveal.
    trend = (inp.daily_velocity_14d / v_raw).quantize(Decimal("0.01"))

    # Days of supply / stockout: RAW. Pessimistic on risk — flag stockouts
    # early so a viral spike still pings the buyer.
    days_of_supply = (Decimal(available) / v_raw).quantize(Decimal("0.1"))
    stockout_date = as_of + timedelta(days=int(days_of_supply))

    # Reorder point + order quantity: ROBUST. Conservative on buying —
    # outlier days don't inflate the projection for two months.
    lead_demand = v * Decimal(inp.lead_time_days)
    safety_buffer = lead_demand * inp.safety_stock_pct
    reorder_point = int((lead_demand + safety_buffer).to_integral_value(rounding="ROUND_HALF_UP"))

    target_units = v * Decimal(inp.lead_time_days + inp.cover_days)
    raw_qty = int((target_units - Decimal(available)).to_integral_value(rounding="ROUND_HALF_UP"))
    raw_qty = max(raw_qty, 0)
    if inp.moq and raw_qty > 0:
        raw_qty = max(raw_qty, inp.moq)
    if inp.case_pack and inp.case_pack > 1 and raw_qty > 0:
        raw_qty = int(ceil(raw_qty / inp.case_pack) * inp.case_pack)
    suggested = raw_qty
    investment = (Decimal(suggested) * inp.unit_cogs).quantize(Decimal("0.01"))

    # Status — earliest-matching wins.
    if not inp.is_reorderable:
        status = ReplenishmentStatus.DISCONTINUED
    elif available <= 0:
        status = ReplenishmentStatus.OUT_OF_STOCK
    elif days_of_supply < inp.lead_time_days:
        # We'll stock out before a PO can arrive.
        status = ReplenishmentStatus.AT_RISK
    elif available < reorder_point:
        status = ReplenishmentStatus.REORDER_NOW
    elif days_of_supply > inp.overstocked_threshold_days:
        status = ReplenishmentStatus.OVERSTOCKED
    else:
        status = ReplenishmentStatus.HEALTHY

    # Suppress reorder quantity for healthy/overstocked/discontinued SKUs —
    # the math may produce a non-zero target if velocity > 0, but we don't
    # want a buy recommendation on a SKU that's already healthy.
    if status in (ReplenishmentStatus.HEALTHY, ReplenishmentStatus.OVERSTOCKED,
                  ReplenishmentStatus.DISCONTINUED, ReplenishmentStatus.NO_VELOCITY):
        suggested = 0
        investment = Decimal("0")

    return ReplenishmentResult(
        sku_code=inp.sku_code, component_sku=inp.component_sku, name=inp.name,
        on_hand=inp.on_hand, expected_receipts=inp.expected_receipts,
        available=available,
        daily_velocity=v, daily_velocity_14d=inp.daily_velocity_14d,
        trend_ratio=trend,
        days_of_supply=days_of_supply, stockout_date=stockout_date,
        lead_time_days=inp.lead_time_days,
        reorder_point=reorder_point,
        suggested_order_qty=suggested,
        investment=investment,
        status=status,
    )


# Status sort order for the replenishment table — most urgent first.
STATUS_PRIORITY = {
    ReplenishmentStatus.OUT_OF_STOCK: 0,
    ReplenishmentStatus.AT_RISK: 1,
    ReplenishmentStatus.REORDER_NOW: 2,
    ReplenishmentStatus.HEALTHY: 3,
    ReplenishmentStatus.OVERSTOCKED: 4,
    ReplenishmentStatus.NO_VELOCITY: 5,
    ReplenishmentStatus.DISCONTINUED: 6,
}
