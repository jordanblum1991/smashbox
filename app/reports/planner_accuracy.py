"""Planner accuracy — surfaces the demand-planner backtest.

`app.services.demand.backtest` already replays what the planner *would* have
recommended at a past `as_of` (using only data ≤ as_of) and scores it against
the actual demand that followed — stockout rates (real vs censored), overstock
rate, and forecast efficiency (actual demand value ÷ recommended investment).
That harness was fully built + tested but never shown anywhere. This wraps it
for a page.

The backtest needs an inventory snapshot at `as_of` AND forward demand after it,
so the earliest meaningful `as_of` is the first snapshot date. We pick the
`as_of` that maximises the forward window while still having a snapshot — it
sharpens as the daily snapshot history accumulates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.inventory_snapshot import InventorySnapshot
from app.services.demand.backtest import (
    CatalogScorecard,
    SkuScorecard,
    _data_max_placed_at,
    score_at,
)


@dataclass
class PlannerAccuracyView:
    has_data: bool
    scorecard: "CatalogScorecard | None" = None
    as_of: date | None = None
    forward_days: int = 0                              # actual-demand window observed
    snapshot_history_from: date | None = None          # earliest snapshot (depth limit)
    verdict: str = ""                                  # one-line interpretation
    verdict_tone: str = "info"                         # info | warn | error
    overstock_skus: list[SkuScorecard] = field(default_factory=list)
    stockout_skus: list[SkuScorecard] = field(default_factory=list)


def compute_planner_accuracy(db: Session, *, horizon_days: int = 30) -> PlannerAccuracyView:
    data_max = _data_max_placed_at(db)
    earliest = db.execute(select(func.min(InventorySnapshot.captured_at))).scalar()
    if data_max is None or earliest is None:
        return PlannerAccuracyView(has_data=False)

    # Latest as_of that still leaves a full horizon of forward demand, but not
    # earlier than our first snapshot (before which there's no on-hand to plan
    # against — the backtest would treat every SKU as empty).
    as_of = max(earliest, data_max - timedelta(days=horizon_days))
    if as_of >= data_max:
        return PlannerAccuracyView(has_data=False, snapshot_history_from=earliest.date())

    sc = score_at(db, as_of=as_of)
    forward_days = (data_max - as_of).days

    overstock = sorted(
        (r for r in sc.per_sku if r.overstock and r.recommended_qty > 0),
        key=lambda r: float(r.recommended_investment - r.actual_demand_value_30d),
        reverse=True,
    )[:8]
    stockouts = [
        r for r in sc.per_sku if r.stockout_during_lead_time and not r.suspected_censored
    ][:8]

    verdict, tone = _verdict(sc)
    return PlannerAccuracyView(
        has_data=True, scorecard=sc, as_of=as_of.date(), forward_days=forward_days,
        snapshot_history_from=earliest.date(), verdict=verdict, verdict_tone=tone,
        overstock_skus=overstock, stockout_skus=stockouts,
    )


def _verdict(sc: CatalogScorecard) -> tuple[str, str]:
    """Plain-English read for tuning safety stock / cover days."""
    eff = sc.forecast_efficiency_30d
    real_stockout = sc.stockout_lead_rate_uncensored
    if real_stockout >= Decimal("10"):
        return (
            f"Under-ordering: {float(real_stockout):.0f}% of stocked SKUs ran out within lead "
            "time. Consider raising safety stock or cover days.", "error",
        )
    if eff and eff < Decimal("0.7"):
        x = (Decimal("1") / eff).quantize(Decimal("0.1")) if eff > 0 else Decimal("0")
        return (
            f"Over-ordering: recommendations were ~{float(x):.1f}× the capital actually "
            f"consumed in {30} days, with no real lead-time stockouts. Safety stock / cover "
            "days can likely come down to free up capital.", "warn",
        )
    return ("Well-calibrated: recommendations tracked actual demand with no material "
            "stockout or overstock skew.", "info")
