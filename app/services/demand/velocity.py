"""Per-SKU sales velocity over a trailing window.

This is the foundational signal the demand planner runs on. We compute it
two ways for the same period:

- **14-day velocity** — recent demand. Picks up viral spikes and sudden
  drops fast.
- **60-day velocity** — baseline demand. Smooths out one-off days, used
  as the headline number for replenishment math.

The planner shows BOTH side-by-side so the buyer can spot SKUs whose
recent trend diverges from baseline.

The 60-day baseline is exposed in two flavours: a *raw* mean (every day
counted at face value) and a *robust* mean (each day clipped at a per-SKU
spike cap before averaging). The robust rate drives buying math; the raw
rate drives stockout/at-risk flags so a viral spike still shows up as risk.

Demand filtering (per the product-requirements answer):
- Only PAID + PAID_SAMPLE order types (real revenue-generating sales)
- Only Order.status in ('Shipped', 'Completed') — canceled/withdrawn
  orders weren't actually demand, just abandoned baskets
- Bundle SKUs are EXPANDED into their components, so a bundle sale shows
  up as demand for each underlying SKU

The output is `{component_sku: daily_velocity}` — keyed by the SKU you
actually need to reorder, not the SKU the customer clicked.
"""
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.order import Order, OrderLine, OrderType
from app.services.demand.bundle_expansion import bundle_component_breakdown
from app.services.sku_alias import load_alias_map


# Order statuses that count as "actual demand shipped/about-to-ship". Anything
# else (Canceled, Withdrawn, Failed, To ship) didn't materially impact stock.
COUNTED_STATUSES = ("Shipped", "Completed")

WINDOW_DAYS = 60


def _median(values: list[int]) -> Decimal:
    """Median of a non-empty list. Returns Decimal."""
    if not values:
        return Decimal("0")
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return Decimal(s[mid])
    return (Decimal(s[mid - 1]) + Decimal(s[mid])) / Decimal(2)


def robust_daily_rate(
    daily_series: list[int],
    *,
    spike_cap_mult: Decimal | None = None,
    raw_mean_mult: Decimal | None = None,
    min_units_for_dampening: int | None = None,
) -> Decimal:
    """Spike-dampened daily rate for a zero-filled daily-units series.

    See `velocity-spike-dampening-spec-short.md`. A day is clipped only when
    it exceeds BOTH `cap_mult × median(non-zero days)` AND
    `mean_mult × raw_mean` — so the two arms protect different regimes:
    the median arm leaves steady SKUs alone; the mean arm protects
    intermittent SKUs whose median is degenerate.

    When the SKU's 60-day total is below the units gate, no dampening is
    applied — the raw rate is returned. This avoids churning low-volume
    SKUs whose noise floor is already below any meaningful cap.
    """
    cap_mult = spike_cap_mult if spike_cap_mult is not None else settings.velocity_spike_cap_mult
    mean_mult = raw_mean_mult if raw_mean_mult is not None else settings.velocity_raw_mean_mult
    min_units = (min_units_for_dampening if min_units_for_dampening is not None
                 else settings.velocity_min_units_for_dampening)

    n = len(daily_series)
    if n == 0:
        return Decimal("0")
    total = sum(daily_series)
    raw_mean = (Decimal(total) / Decimal(n))

    if total < min_units:
        return raw_mean.quantize(Decimal("0.01"))

    median_nz = _median([d for d in daily_series if d > 0])
    cap_day = max(cap_mult * median_nz, mean_mult * raw_mean)

    clipped_total = sum((min(Decimal(d), cap_day) for d in daily_series), Decimal("0"))
    return (clipped_total / Decimal(n)).quantize(Decimal("0.01"))


@dataclass
class SkuVelocity:
    """Per-component velocity over the trailing window."""
    component_sku: str
    units_14d: int          # raw units in last 14d (post-expansion)
    units_60d: int          # raw units in last 60d (post-expansion)
    daily_series_60d: list[int] = field(default_factory=list)  # zero-filled 60-day series

    @property
    def daily_14d(self) -> Decimal:
        return (Decimal(self.units_14d) / Decimal(14)).quantize(Decimal("0.01"))

    @property
    def daily_60d_raw(self) -> Decimal:
        """Flat 60-day mean — used for days-of-supply, stockout dates, and
        the at-risk/out-of-stock flags so a viral spike surfaces as risk."""
        return (Decimal(self.units_60d) / Decimal(WINDOW_DAYS)).quantize(Decimal("0.01"))

    # Back-compat alias: `daily_60d` historically meant the raw mean. Kept so
    # callers that don't care about the raw/robust split keep working.
    @property
    def daily_60d(self) -> Decimal:
        return self.daily_60d_raw

    @property
    def daily_60d_robust(self) -> Decimal:
        """Spike-dampened 60-day rate — drives reorder point, suggested qty,
        and the investment outlook so one outlier day doesn't trigger
        overbuying for two months."""
        return robust_daily_rate(self.daily_series_60d)

    @property
    def sigma_daily_raw(self) -> Decimal:
        """Standard deviation of the RAW (uncapped) 60-day daily series.

        Used by variance-based safety stock. Critical that this comes from
        the uncapped series — the spike cap reduces σ by clipping the very
        outlier days the buffer is meant to insure against. Using capped σ
        would under-buffer the exact volatility we're trying to absorb.

        Sample standard deviation (n−1 denominator) since we're estimating
        the underlying demand process's σ from a 60-day window of
        observations, not treating the window as the full population.
        """
        n = len(self.daily_series_60d)
        if n < 2:
            return Decimal("0")
        mean = Decimal(sum(self.daily_series_60d)) / Decimal(n)
        sum_sq = sum((Decimal(x) - mean) ** 2 for x in self.daily_series_60d)
        variance = sum_sq / Decimal(n - 1)
        return Decimal(str(math.sqrt(float(variance)))).quantize(Decimal("0.0001"))

    @property
    def trend_ratio(self) -> Decimal:
        """14-day / 60-day daily rate, both RAW. Surfaces real demand shape
        without the cap muddying the signal. Returns 1.0 when 60-day rate
        is zero (no signal to compare against)."""
        raw = self.daily_60d_raw
        if raw == 0:
            return Decimal("1")
        return (self.daily_14d / raw).quantize(Decimal("0.01"))


def _daily_units_by_sku(
    db: Session, start: datetime, end: datetime,
    *, alias_map: dict[str, str] | None = None,
) -> dict[str, dict[date, int]]:
    """Per-SKU per-day units for shipped/completed PAID orders in [start, end).
    Pre-bundle-expansion. Returns `{order_line.sku: {date: units}}`.

    When `alias_map` is provided, aliased SKUs collapse to their canonical
    BEFORE the daily series is built — so demand history for a re-coded
    product (e.g. `C09D01` and `SBX-C09D01`) combines into one signal."""
    rows = db.execute(
        select(OrderLine.sku, OrderLine.quantity, Order.placed_at)
        .join(Order, Order.id == OrderLine.order_id)
        .where(Order.placed_at >= start, Order.placed_at < end)
        .where(Order.order_type.in_([OrderType.PAID, OrderType.PAID_SAMPLE]))
        .where(Order.status.in_(COUNTED_STATUSES))
    ).all()
    out: dict[str, dict[date, int]] = defaultdict(lambda: defaultdict(int))
    alias_map = alias_map or {}
    for sku, qty, placed_at in rows:
        canonical = alias_map.get(sku, sku)
        out[canonical][placed_at.date()] += int(qty or 0)
    return {k: dict(v) for k, v in out.items()}


def _expand_daily_to_components(
    db: Session, daily_by_sku: dict[str, dict[date, int]],
    *, alias_map: dict[str, str] | None = None,
) -> dict[str, dict[date, int]]:
    """Translate `{order_line.sku: {date: units}}` into
    `{component_sku: {date: units}}` by exploding bundle SKUs into their
    constituent component SKUs. A bundle sale of "Kit A" (qty=N) with
    components 1× Foo + 1× Bar adds N units of demand to Foo AND N units
    of demand to Bar on that day. A single-SKU sale passes through to its
    own component bucket.

    `alias_map` is also applied to bundle COMPONENTS after expansion — so a
    legacy component code in the bundle catalog still rolls up into the
    canonical SKU's daily series."""
    if not daily_by_sku:
        return {}

    alias_map = alias_map or {}
    bundle_map = bundle_component_breakdown(db, set(daily_by_sku))

    out: dict[str, dict[date, int]] = defaultdict(lambda: defaultdict(int))
    for sku_key, by_day in daily_by_sku.items():
        components = bundle_map.get(sku_key)
        if components:
            for component_sku, qty_per_bundle in components:
                canonical_component = alias_map.get(component_sku, component_sku)
                for d, units in by_day.items():
                    out[canonical_component][d] += units * qty_per_bundle
        else:
            for d, units in by_day.items():
                out[sku_key][d] += units
    return {k: dict(v) for k, v in out.items()}


def compute_velocity(
    db: Session, *, as_of: datetime,
    alias_map: dict[str, str] | None = None,
) -> dict[str, SkuVelocity]:
    """Per-component daily velocity over trailing 60d as of `as_of`, with
    a 14d sub-window derived from the same series.

    Window is anchored to midnight so the daily series has clean date
    boundaries — a query at 14:00 still slots events into whole-day buckets.

    `alias_map` (default: lazy-loaded from `sku_aliases` table) collapses
    re-coded SKUs to their canonical before daily bucketing, so demand
    history for a renamed product isn't split across two codes. Pass an
    explicit `{}` to disable for tests or one-off analyses.

    Returns a dict keyed by the component SKU (the SKU you reorder against).
    """
    if alias_map is None:
        alias_map = load_alias_map(db)

    end_date = as_of.date()
    start_date = end_date - timedelta(days=WINDOW_DAYS)
    start_dt = datetime(start_date.year, start_date.month, start_date.day)
    end_dt = datetime(end_date.year, end_date.month, end_date.day)

    raw_daily = _daily_units_by_sku(db, start_dt, end_dt, alias_map=alias_map)
    comp_daily = _expand_daily_to_components(db, raw_daily, alias_map=alias_map)

    out: dict[str, SkuVelocity] = {}
    for component_sku, by_day in comp_daily.items():
        series_60 = [by_day.get(start_date + timedelta(days=i), 0)
                     for i in range(WINDOW_DAYS)]
        units_60 = sum(series_60)
        if units_60 == 0:
            # No demand at all — skip rather than emit a zero-velocity row
            # the planner has to filter back out.
            continue
        out[component_sku] = SkuVelocity(
            component_sku=component_sku,
            units_14d=sum(series_60[-14:]),
            units_60d=units_60,
            daily_series_60d=series_60,
        )
    return out
