"""Per-SKU sales velocity over a trailing window.

This is the foundational signal the demand planner runs on. We compute it
two ways for the same period:

- **14-day velocity** — recent demand. Picks up viral spikes and sudden
  drops fast.
- **60-day velocity** — baseline demand. Smooths out one-off days, used
  as the headline number for replenishment math.

The planner shows BOTH side-by-side so the buyer can spot SKUs whose
recent trend diverges from baseline.

Demand filtering (per the product-requirements answer):
- Only PAID + PAID_SAMPLE order types (real revenue-generating sales)
- Only Order.status in ('Shipped', 'Completed') — canceled/withdrawn
  orders weren't actually demand, just abandoned baskets
- Bundle SKUs are EXPANDED into their components, so a bundle sale shows
  up as demand for each underlying SKU

The output is `{component_sku: daily_velocity}` — keyed by the SKU you
actually need to reorder, not the SKU the customer clicked.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.order import Order, OrderLine, OrderType
from app.services.demand.bundle_expansion import bundle_component_breakdown


# Order statuses that count as "actual demand shipped/about-to-ship". Anything
# else (Canceled, Withdrawn, Failed, To ship) didn't materially impact stock.
COUNTED_STATUSES = ("Shipped", "Completed")


@dataclass
class SkuVelocity:
    """Per-component velocity over the trailing window."""
    component_sku: str
    units_14d: int          # raw units in last 14d (post-expansion)
    units_60d: int          # raw units in last 60d (post-expansion)

    @property
    def daily_14d(self) -> Decimal:
        return (Decimal(self.units_14d) / Decimal(14)).quantize(Decimal("0.01"))

    @property
    def daily_60d(self) -> Decimal:
        return (Decimal(self.units_60d) / Decimal(60)).quantize(Decimal("0.01"))

    @property
    def trend_ratio(self) -> Decimal:
        """14-day / 60-day daily rate. >1 = accelerating, <1 = decelerating.
        Returns 1.0 when 60-day rate is zero (no signal to compare against)."""
        if self.daily_60d == 0:
            return Decimal("1")
        return (self.daily_14d / self.daily_60d).quantize(Decimal("0.01"))


def _units_by_sku_in_window(
    db: Session, start: datetime, end: datetime
) -> dict[str, int]:
    """Raw `{order_line.sku: units}` for shipped/completed PAID orders in
    [start, end). Pre-bundle-expansion."""
    rows = db.execute(
        select(OrderLine.sku, func.coalesce(func.sum(OrderLine.quantity), 0))
        .join(Order, Order.id == OrderLine.order_id)
        .where(Order.placed_at >= start, Order.placed_at < end)
        .where(Order.order_type.in_([OrderType.PAID, OrderType.PAID_SAMPLE]))
        .where(Order.status.in_(COUNTED_STATUSES))
        .group_by(OrderLine.sku)
    ).all()
    return {sku: int(qty or 0) for sku, qty in rows}


def _expand_to_components(
    db: Session, units_by_sku: dict[str, int]
) -> dict[str, int]:
    """Translate `{order_line.sku: units}` into `{component_sku: units}` by
    exploding bundle SKUs into the SKUs of their constituent components.

    A bundle sale of "Kit A" (qty=2) with components 1× Foo + 1× Bar adds
    2 units of demand to Foo AND 2 units of demand to Bar. A single-SKU
    sale (no bundle row matches) passes through to its own component
    bucket.
    """
    if not units_by_sku:
        return {}

    bundle_map = bundle_component_breakdown(db, set(units_by_sku))

    out: dict[str, int] = defaultdict(int)
    for sku_key, units in units_by_sku.items():
        if units <= 0:
            continue
        components = bundle_map.get(sku_key)
        if components:
            for component_sku, qty_per_bundle in components:
                out[component_sku] += units * qty_per_bundle
        else:
            # Non-bundle: this IS the component SKU itself.
            out[sku_key] += units
    return dict(out)


def compute_velocity(db: Session, *, as_of: datetime) -> dict[str, SkuVelocity]:
    """Per-component daily velocity over trailing 14d and 60d as of `as_of`.

    Returns a dict keyed by the component SKU (the SKU you reorder against).
    """
    end = as_of
    start_14 = end - timedelta(days=14)
    start_60 = end - timedelta(days=60)

    raw_14 = _units_by_sku_in_window(db, start_14, end)
    raw_60 = _units_by_sku_in_window(db, start_60, end)

    comp_14 = _expand_to_components(db, raw_14)
    comp_60 = _expand_to_components(db, raw_60)

    all_components = set(comp_14) | set(comp_60)
    return {
        sku: SkuVelocity(
            component_sku=sku,
            units_14d=comp_14.get(sku, 0),
            units_60d=comp_60.get(sku, 0),
        )
        for sku in all_components
    }
