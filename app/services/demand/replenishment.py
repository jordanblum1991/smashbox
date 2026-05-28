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
import math
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum
from math import ceil

from app.config import settings


class ReplenishmentStatus(str, Enum):
    OUT_OF_STOCK = "out_of_stock"
    AT_RISK = "at_risk"
    REORDER_NOW = "reorder_now"
    HEALTHY = "healthy"
    OVERSTOCKED = "overstocked"
    DISCONTINUED = "discontinued"
    NO_VELOCITY = "no_velocity"   # never sold in the window — can't compute


class TrendDirection(str, Enum):
    """How the 14-day rate compares to the 60-day baseline. ACCELERATING
    fires the trend-adjusted ROP branch; DECELERATING is informational only
    (asymmetric by design — see settings.demand_trend_acceleration_threshold).
    INSUFFICIENT_DATA covers no-velocity SKUs and cold-start SKUs where the
    14-day window doesn't contain enough history to be meaningful.
    """
    ACCELERATING = "accelerating"
    DECELERATING = "decelerating"
    STABLE = "stable"
    INSUFFICIENT_DATA = "insufficient_data"


def poisson_safety_stock(mean_lead_demand: Decimal, service_level: Decimal) -> int:
    """Safety stock for slow-mover SKUs whose demand is Poisson rather than
    Gaussian. Returns `ppf(service_level, μ·L) − μ·L`, i.e. the buffer needed
    so cumulative P(demand ≤ μ·L + buffer) ≥ service_level.

    Pure-Python iterative implementation — Poisson PMF is built up from k=0
    until cumulative probability crosses the threshold. For slow movers
    (μ·L typically < 15) convergence is fast (~20 iterations); the k<200
    sanity cap guards against pathological inputs and never fires in practice.

    Returns an int >= 0. When μ·L is 0 or the inputs are degenerate, returns 0.
    """
    mu_l = float(mean_lead_demand)
    target = float(service_level)
    if mu_l <= 0 or target <= 0:
        return 0

    cumulative = math.exp(-mu_l)   # P(X = 0)
    pmf = cumulative
    k = 0
    while cumulative < target and k < 200:
        k += 1
        pmf *= mu_l / k
        cumulative += pmf
    safety = k - mu_l
    if safety < 0:
        return 0
    return int(round(safety))


def _service_level_for(z_value: Decimal | None, fallback: Decimal | None) -> Decimal | None:
    """Best-effort service level for the Poisson PPF call. Prefers an explicit
    `service_level` from inputs; falls back to reverse-lookup from `z_value`
    via the canonical table. Returns None if neither resolves — Poisson is
    then skipped and the caller falls back to variance / flat.
    """
    if fallback is not None:
        return fallback
    if z_value is None:
        return None
    from app.config import SERVICE_LEVEL_Z_TABLE
    for sl, z in SERVICE_LEVEL_Z_TABLE.items():
        if z == z_value:
            return sl
    return None


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

    # Variance-based safety stock inputs. When `sigma_daily` is provided and
    # non-zero, safety_stock = z × σ × √lead_time (the demand-variability
    # model). Otherwise we fall back to the flat `safety_stock_pct` method
    # (legacy behaviour, preserved so the unit tests and any caller that
    # doesn't supply σ keep working).
    #
    # `sigma_daily` MUST be the σ of the RAW (uncapped) daily series — the
    # spike cap shrinks σ and would under-buffer the spikes we're insuring
    # against. The caller in demand_planning.py reads SkuVelocity.sigma_daily_raw.
    sigma_daily: Decimal | None = None
    z_value: Decimal | None = None
    # Explicit service level (e.g. 0.95) used for Poisson PPF on slow movers.
    # Optional — when omitted we reverse-lookup from z_value via the table.
    service_level: Decimal | None = None

    # Cold-start inputs. `days_observed` is the number of days the SKU has
    # existed within the 60-day window (= WINDOW_DAYS for mature SKUs). When
    # below `settings.demand_cold_start_threshold_days`, the math re-means
    # over observed days only and applies `cold_start_uplift`. Defaults
    # preserve mature-SKU behavior for callers that don't supply these.
    days_observed: int = 60
    cold_start_uplift: Decimal | None = None  # None → settings default at use site

    # Units actually sold in the window — needed so the cold-start branch
    # can compute `daily_observed = units_observed / days_observed`. Defaults
    # to None for callers that don't have it (in which case cold-start can
    # still apply by using `daily_velocity * 60 / days_observed` as the
    # numerator-equivalent reconstruction).
    units_observed: int | None = None


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

    # How the safety buffer was actually computed for this row — visible on
    # the drill-down "How the suggestion was computed" panel so the buyer
    # can see whether variance, Poisson, or the flat % fallback applied.
    # `method` is one of: "variance" (z × σ × √L), "poisson"
    # (Poisson PPF for slow movers), or "flat" (lead_demand × pct).
    safety_stock_units: int = 0
    safety_method: str = "flat"

    # How the velocity baseline was computed. "standard" = robust 60-day
    # mean. "cold_start" = the SKU was sold for the first time within the
    # 60-day window, so velocity is `units_observed / days_observed` × uplift.
    velocity_method: str = "standard"

    # 14d-vs-60d trend classification. Asymmetric in math: ACCELERATING
    # triggers the trend-adjusted ROP branch; DECELERATING is a UI signal
    # only (deceleration does NOT shrink ROP).
    trend_direction: TrendDirection = TrendDirection.STABLE

    # True iff the ROP base velocity was blended with the 14d rate because
    # trend_direction was ACCELERATING. Visible on the drill-down so the
    # buyer can see why ROP suddenly bumped.
    trend_adjustment_applied: bool = False


def compute_one(inp: ReplenishmentInputs, *, as_of: date) -> ReplenishmentResult:
    """Single-SKU replenishment calculation.

    Velocity flow:
      v_raw     — the displayed 60d (or observed-days, for cold-start) baseline.
                  Drives trend_ratio and days_of_supply.
      v         — the ROBUST rate driving SOQ. For cold-start SKUs this is
                  `daily_observed × uplift`; for mature SKUs it's the
                  spike-dampened 60d mean.
      v_for_rop — v, optionally up-blended with the 14d rate when the SKU is
                  ACCELERATING. Used only for ROP base + safety stock base.

    Safety-stock branches (mutually exclusive, in order):
      poisson   — when v_for_rop < settings.demand_slow_mover_threshold AND a
                  service level resolves. Lead-time demand is Poisson(μ·L).
      variance  — when σ > 0 AND z is supplied. safety = z × σ × √L.
      flat      — fallback: safety = lead_demand × safety_stock_pct.
    """
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
            trend_direction=TrendDirection.INSUFFICIENT_DATA,
        )

    # Cold-start: if the SKU has fewer than the threshold days of history,
    # replace v with `daily_observed × uplift`. Daily-observed reconstructed
    # from units_observed when provided, else from daily_velocity_raw ×
    # (WINDOW / days_observed) — preserves callers that don't supply units.
    cold_start_threshold = settings.demand_cold_start_threshold_days
    velocity_method = "standard"
    is_cold_start = (
        inp.days_observed is not None
        and 0 < inp.days_observed < cold_start_threshold
    )
    if is_cold_start:
        uplift = (inp.cold_start_uplift
                  if inp.cold_start_uplift is not None
                  else settings.demand_cold_start_uplift)
        if inp.units_observed is not None and inp.days_observed > 0:
            daily_observed = Decimal(inp.units_observed) / Decimal(inp.days_observed)
        else:
            # Reconstruct from v_raw, which the caller computed as
            # units / WINDOW_DAYS for mature SKUs. For a cold-start SKU we
            # need units / days_observed, so multiply back up by the ratio.
            daily_observed = (v_raw * Decimal(60)) / Decimal(inp.days_observed)
        v = (daily_observed * uplift).quantize(Decimal("0.01"))
        velocity_method = "cold_start"
        # v_raw also moves to the observed-days denominator — pre-existence
        # zero days were polluting days_of_supply too.
        v_raw = daily_observed.quantize(Decimal("0.01"))

    # Trend ratio: RAW vs RAW. Surfaces real demand shape — clipping would
    # muddy the very signal the trend is meant to reveal.
    trend = (inp.daily_velocity_14d / v_raw).quantize(Decimal("0.01"))

    # Trend direction. Cold-start SKUs have INSUFFICIENT_DATA regardless of
    # the ratio — the 14d window contains too few "post-existence" days for
    # the comparison to be meaningful. For mature SKUs the threshold is
    # symmetric around 1 (acceleration = ratio > T; deceleration = ratio < 1/T).
    # STRICT inequalities: ratio at exactly T is STABLE, only strictly past it
    # classifies as ACCELERATING or DECELERATING. Matches the spec.
    accel_threshold = settings.demand_trend_acceleration_threshold
    decel_threshold = Decimal("1") / accel_threshold
    if is_cold_start:
        trend_direction = TrendDirection.INSUFFICIENT_DATA
    elif trend > accel_threshold:
        trend_direction = TrendDirection.ACCELERATING
    elif trend < decel_threshold:
        trend_direction = TrendDirection.DECELERATING
    else:
        trend_direction = TrendDirection.STABLE

    # Days of supply / stockout: RAW. Pessimistic on risk — flag stockouts
    # early so a viral spike still pings the buyer.
    days_of_supply = (Decimal(available) / v_raw).quantize(Decimal("0.1"))
    stockout_date = as_of + timedelta(days=int(days_of_supply))

    # Trend-adjusted ROP base velocity. Only blends up on ACCELERATING —
    # asymmetric by design (deceleration shrinking ROP would risk stockouts
    # on a recovery). The blend weights are settings-driven.
    trend_adjustment_applied = False
    if trend_direction == TrendDirection.ACCELERATING:
        w_recent = settings.demand_trend_weight_recent
        v_for_rop = (w_recent * inp.daily_velocity_14d
                     + (Decimal("1") - w_recent) * v).quantize(Decimal("0.01"))
        trend_adjustment_applied = True
    else:
        v_for_rop = v

    # Reorder point base. v_for_rop already incorporates cold-start uplift
    # (via v) and trend acceleration (when applicable).
    lead_demand = v_for_rop * Decimal(inp.lead_time_days)

    # Safety stock branch selection. Order matters: Poisson supersedes
    # variance for slow movers; variance supersedes flat when σ is available.
    slow_mover_threshold = settings.demand_slow_mover_threshold
    is_slow_mover = v_for_rop < slow_mover_threshold
    poisson_sl = _service_level_for(inp.z_value, inp.service_level)

    if is_slow_mover and poisson_sl is not None:
        safety_stock_units = poisson_safety_stock(lead_demand, poisson_sl)
        safety_buffer = Decimal(safety_stock_units)
        safety_method = "poisson"
    elif inp.sigma_daily is not None and inp.sigma_daily > 0 and inp.z_value is not None:
        sqrt_lead = Decimal(str(math.sqrt(inp.lead_time_days)))
        safety_buffer = inp.z_value * inp.sigma_daily * sqrt_lead
        safety_stock_units = int(safety_buffer.to_integral_value(rounding="ROUND_HALF_UP"))
        safety_method = "variance"
    else:
        safety_buffer = lead_demand * inp.safety_stock_pct
        safety_stock_units = int(safety_buffer.to_integral_value(rounding="ROUND_HALF_UP"))
        safety_method = "flat"
    reorder_point = int((lead_demand + safety_buffer).to_integral_value(rounding="ROUND_HALF_UP"))

    # Suggested order qty uses v (cold-start-adjusted but NOT trend-blended).
    # Trend blending intentionally affects ROP only — we don't want to chase
    # a 14-day spike across 60 days of forward cover.
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
        safety_stock_units=safety_stock_units,
        safety_method=safety_method,
        velocity_method=velocity_method,
        trend_direction=trend_direction,
        trend_adjustment_applied=trend_adjustment_applied,
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
