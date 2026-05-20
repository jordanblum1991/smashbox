"""Backtest harness for the demand planning system.

Replays history: pick an `as_of` date in the past, ask the live planner what
it WOULD have recommended on that date (using only data the planner would
have had access to back then), then measure what actually happened in the
days that followed.

This is measurement only — it imports and calls `compute_velocity` and
`compute_one` directly. The math is not re-implemented and not modified.

The one extra constraint this module imposes is on the inventory snapshot:
the live planner reads the most-recent snapshot regardless of date, but
for a backtest that would leak future information. So this module fetches
the most-recent snapshot **at-or-before** `as_of` per SKU.

For each scored SKU we compute:
  - on-hand at as_of (closest snapshot <= as_of)
  - what the planner would have recommended at as_of (suggested_order_qty)
  - actual demand in [as_of, as_of + lead_time), 30d, and 60d windows
  - stockout flags: actual demand exceeded the planner's expected supply
  - overstock flag: recommended quantity was >2x what actually sold

Outputs both per-SKU rows and a catalog-level roll-up.

CLI:
    python -m app.services.demand.backtest               # last 3 month-starts
    python -m app.services.demand.backtest 2026-03-01    # one specific date
    python -m app.services.demand.backtest 2026-03-01 2026-04-01 2026-05-01
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.inventory_snapshot import InventorySnapshot
from app.models.order import Order
from app.models.sku import Sku
from app.reports.demand_planning import _sku_by_component
from app.services.demand.replenishment import (
    ReplenishmentInputs,
    ReplenishmentResult,
    ReplenishmentStatus,
    compute_one,
)
from app.services.demand.velocity import (
    _daily_units_by_sku,
    _expand_daily_to_components,
    compute_velocity,
)


# Defaults — match the production planner.
DEFAULT_OVERSTOCK_MULTIPLE = Decimal("2.0")


@dataclass
class SkuScorecard:
    """Per-SKU backtest result for one as_of date."""
    component_sku: str
    sku_code: str | None
    name: str | None

    # State at as_of (what the planner saw)
    on_hand_at_as_of: int
    recommended_qty: int
    daily_velocity_at_as_of: Decimal           # 60d_robust
    unit_cogs: Decimal
    lead_time_days: int
    status: ReplenishmentStatus

    # Actuals in [as_of, as_of + window)
    actual_demand_lead: int
    actual_demand_30d: int
    actual_demand_60d: int

    # Diagnoses
    stockout_during_lead_time: bool   # actual_lead > on_hand (PO didn't arrive in time)
    stockout_at_30d: bool             # actual_30d > on_hand + recommended_qty
    stockout_at_60d: bool             # actual_60d > on_hand + recommended_qty
    overstock: bool                   # recommended > overstock_multiple × actual_30d

    @property
    def available_after_po(self) -> int:
        return self.on_hand_at_as_of + self.recommended_qty

    @property
    def recommended_investment(self) -> Decimal:
        return (Decimal(self.recommended_qty) * self.unit_cogs).quantize(Decimal("0.01"))

    @property
    def actual_demand_value_30d(self) -> Decimal:
        return (Decimal(self.actual_demand_30d) * self.unit_cogs).quantize(Decimal("0.01"))

    @property
    def actual_demand_value_60d(self) -> Decimal:
        return (Decimal(self.actual_demand_60d) * self.unit_cogs).quantize(Decimal("0.01"))


@dataclass
class CatalogScorecard:
    """Catalog-level roll-up for one as_of date."""
    as_of: date
    skus_scored: int
    skus_with_recommendation: int     # qty > 0; denominator for overstock + stockout-at-30d

    stockout_lead_count: int
    stockout_30d_count: int
    stockout_60d_count: int
    overstock_count: int

    total_recommended_investment: Decimal
    total_actual_demand_value_30d: Decimal
    total_actual_demand_value_60d: Decimal

    # Window coverage — how much post-as_of data we actually have.
    # 60 means the full 60-day actuals window is in-data; less means
    # the harness is scoring against partial history.
    days_of_actuals_available: int

    per_sku: list[SkuScorecard] = field(default_factory=list)

    @property
    def stockout_lead_rate(self) -> Decimal:
        return _rate(self.stockout_lead_count, self.skus_scored)

    @property
    def stockout_30d_rate(self) -> Decimal:
        return _rate(self.stockout_30d_count, self.skus_with_recommendation)

    @property
    def stockout_60d_rate(self) -> Decimal:
        return _rate(self.stockout_60d_count, self.skus_with_recommendation)

    @property
    def overstock_rate(self) -> Decimal:
        return _rate(self.overstock_count, self.skus_with_recommendation)

    @property
    def forecast_efficiency_30d(self) -> Decimal:
        """Actual demand value / recommended investment. 1.0 ≈ perfect;
        below 1.0 means we recommended more capital than the next 30d
        actually consumed (over-ordering); above 1.0 means we
        under-recommended (potential lost sales)."""
        if self.total_recommended_investment <= 0:
            return Decimal("0")
        return (self.total_actual_demand_value_30d / self.total_recommended_investment).quantize(Decimal("0.01"))


def _rate(num: int, denom: int) -> Decimal:
    if denom <= 0:
        return Decimal("0")
    return (Decimal(num) / Decimal(denom) * Decimal(100)).quantize(Decimal("0.1"))


def historical_on_hand(db: Session, as_of: datetime) -> dict[str, int]:
    """Most-recent on-hand per SKU with captured_at <= as_of. SKUs never
    snapshotted (or only snapshotted after as_of) are absent from the dict.
    A backtest treats those as 0 on-hand."""
    latest = (
        select(
            InventorySnapshot.sku.label("sku"),
            func.max(InventorySnapshot.captured_at).label("max_at"),
        )
        .where(InventorySnapshot.captured_at <= as_of)
        .group_by(InventorySnapshot.sku)
        .subquery()
    )
    rows = db.execute(
        select(InventorySnapshot.sku, InventorySnapshot.on_hand)
        .join(
            latest,
            (InventorySnapshot.sku == latest.c.sku)
            & (InventorySnapshot.captured_at == latest.c.max_at),
        )
    ).all()
    return {sku: int(oh or 0) for sku, oh in rows}


def historical_recommendations(
    db: Session, *, as_of: datetime,
    safety_stock_pct: Decimal | None = None,
    cover_days: int | None = None,
    overstocked_days: int | None = None,
) -> dict[str, tuple[ReplenishmentResult, Decimal]]:
    """What `compute_demand_planning_view` would have returned at this point
    in time, given a time-filtered inventory snapshot.

    Returns `{component_sku: (ReplenishmentResult, unit_cogs)}`. The unit_cogs
    is included because ReplenishmentResult doesn't carry it on its own — we
    need it for catalog investment + actual-demand-value math.

    Reuses every input the live planner uses (per-SKU procurement attrs,
    safety stock, cover days, etc.). The only divergence from live is the
    snapshot filter — production reads the most-recent snapshot regardless
    of date, which would leak future info into the backtest.
    """
    safety = safety_stock_pct if safety_stock_pct is not None else settings.demand_safety_stock_pct
    cover = cover_days if cover_days is not None else settings.demand_cover_days
    overstocked = overstocked_days if overstocked_days is not None else settings.demand_overstocked_days

    velocities = compute_velocity(db, as_of=as_of)
    on_hand_map = historical_on_hand(db, as_of)
    all_skus = set(velocities) | set(on_hand_map)
    if not all_skus:
        return {}

    sku_meta = _sku_by_component(db, all_skus)

    out: dict[str, tuple[ReplenishmentResult, Decimal]] = {}
    for component_sku in all_skus:
        v = velocities.get(component_sku)
        s = sku_meta.get(component_sku)
        on_hand = on_hand_map.get(component_sku, 0)

        lead_time = (s.lead_time_days if s and s.lead_time_days
                     else settings.demand_lead_time_default_days)
        moq = (s.moq or 0) if s else 0
        case_pack = (s.case_pack or 0) if s else 0
        sku_safety_pct = None
        if s and s.safety_stock_pct is not None:
            try:
                sku_safety_pct = Decimal(str(s.safety_stock_pct)) / Decimal("100")
            except Exception:  # noqa: BLE001
                sku_safety_pct = None
        effective_safety = sku_safety_pct if sku_safety_pct is not None else safety
        is_reorderable = True if not s else (
            s.is_reorderable if s.is_reorderable is not None else True
        )
        unit_cogs = Decimal(str(s.unit_cogs)) if (s and s.unit_cogs) else Decimal("0")

        inputs = ReplenishmentInputs(
            sku_code=(s.sku if s else None),
            component_sku=component_sku,
            name=(s.name if s else None),
            on_hand=on_hand,
            expected_receipts=0,
            daily_velocity=v.daily_60d_robust if v else Decimal("0"),
            daily_velocity_raw=v.daily_60d_raw if v else Decimal("0"),
            daily_velocity_14d=v.daily_14d if v else Decimal("0"),
            lead_time_days=lead_time,
            safety_stock_pct=effective_safety,
            cover_days=cover,
            overstocked_threshold_days=overstocked,
            moq=moq,
            case_pack=case_pack,
            is_reorderable=is_reorderable,
            unit_cogs=unit_cogs,
        )
        result = compute_one(inputs, as_of=as_of.date())
        out[component_sku] = (result, unit_cogs)

    return out


def actual_demand_post_as_of(
    db: Session, *, as_of: datetime, horizon_days: int
) -> dict[str, dict[date, int]]:
    """Bundle-expanded daily demand for [as_of, as_of + horizon_days), per
    component SKU. Reuses the velocity service's filtering rules (PAID/
    PAID_SAMPLE + Shipped/Completed) and bundle-expansion logic so 'actual'
    is measured on the same definition the planner forecasts against."""
    end = as_of + timedelta(days=horizon_days)
    raw_daily = _daily_units_by_sku(db, as_of, end)
    return _expand_daily_to_components(db, raw_daily)


def _data_max_placed_at(db: Session) -> datetime | None:
    return db.execute(select(func.max(Order.placed_at))).scalar()


def score_at(
    db: Session,
    *,
    as_of: datetime,
    overstock_multiple: Decimal = DEFAULT_OVERSTOCK_MULTIPLE,
    safety_stock_pct: Decimal | None = None,
    cover_days: int | None = None,
) -> CatalogScorecard:
    """Build a scorecard for one historical as_of date."""
    recs = historical_recommendations(
        db, as_of=as_of,
        safety_stock_pct=safety_stock_pct,
        cover_days=cover_days,
    )

    # Determine how much post-window data we actually have. Score the
    # 60-day window if it's in-data; report partial coverage otherwise.
    # If data extends to date D and as_of is date A, we have data for dates
    # A, A+1, ..., D inclusive = (D - A) + 1 dates. Used to clip horizon
    # windows so we don't count missing-data days as "zero demand."
    data_max = _data_max_placed_at(db)
    days_available = 60
    if data_max is not None:
        days_available = max(0, min(60, (data_max.date() - as_of.date()).days + 1))

    # One query for daily demand over the full 60-day horizon — slice per
    # SKU rather than re-query for each lead_time / 30d / 60d.
    daily_demand = actual_demand_post_as_of(db, as_of=as_of, horizon_days=60)

    def _sum_demand(component_sku: str, n_days: int) -> int:
        by_day = daily_demand.get(component_sku, {})
        start_d = as_of.date()
        # Cap at data_max so we don't fabricate zeros past the dataset edge.
        cap = min(n_days, days_available) if days_available is not None else n_days
        return sum(by_day.get(start_d + timedelta(days=i), 0) for i in range(cap))

    per_sku: list[SkuScorecard] = []
    skus_with_rec = 0
    stockout_lead = stockout_30d = stockout_60d = overstock = 0
    total_recommended = Decimal("0")
    total_actual_30d = Decimal("0")
    total_actual_60d = Decimal("0")

    for component_sku, (r, unit_cogs) in recs.items():
        d_lead = _sum_demand(component_sku, r.lead_time_days)
        d30 = _sum_demand(component_sku, 30)
        d60 = _sum_demand(component_sku, 60)

        available = r.on_hand + r.suggested_order_qty
        stockout_lead_b = d_lead > r.on_hand
        stockout_30_b = r.suggested_order_qty > 0 and d30 > available
        stockout_60_b = r.suggested_order_qty > 0 and d60 > available
        # Overstock: recommended >> actual. Two sub-cases:
        #  (a) we recommended units for a SKU that sold zero in 30 days
        #  (b) we recommended more than overstock_multiple × actual_30d
        overstock_b = False
        if r.suggested_order_qty > 0:
            if d30 == 0:
                overstock_b = True
            elif Decimal(r.suggested_order_qty) > overstock_multiple * Decimal(d30):
                overstock_b = True

        sc = SkuScorecard(
            component_sku=component_sku,
            sku_code=r.sku_code,
            name=r.name,
            on_hand_at_as_of=r.on_hand,
            recommended_qty=r.suggested_order_qty,
            daily_velocity_at_as_of=r.daily_velocity,
            unit_cogs=unit_cogs,
            lead_time_days=r.lead_time_days,
            status=r.status,
            actual_demand_lead=d_lead,
            actual_demand_30d=d30,
            actual_demand_60d=d60,
            stockout_during_lead_time=stockout_lead_b,
            stockout_at_30d=stockout_30_b,
            stockout_at_60d=stockout_60_b,
            overstock=overstock_b,
        )
        per_sku.append(sc)

        if r.suggested_order_qty > 0:
            skus_with_rec += 1
        if stockout_lead_b:
            stockout_lead += 1
        if stockout_30_b:
            stockout_30d += 1
        if stockout_60_b:
            stockout_60d += 1
        if overstock_b:
            overstock += 1
        total_recommended += sc.recommended_investment
        total_actual_30d += sc.actual_demand_value_30d
        total_actual_60d += sc.actual_demand_value_60d

    return CatalogScorecard(
        as_of=as_of.date(),
        skus_scored=len(per_sku),
        skus_with_recommendation=skus_with_rec,
        stockout_lead_count=stockout_lead,
        stockout_30d_count=stockout_30d,
        stockout_60d_count=stockout_60d,
        overstock_count=overstock,
        total_recommended_investment=total_recommended,
        total_actual_demand_value_30d=total_actual_30d,
        total_actual_demand_value_60d=total_actual_60d,
        days_of_actuals_available=days_available,
        per_sku=per_sku,
    )


def sweep(
    db: Session,
    as_of_dates: list[datetime],
    *,
    overstock_multiple: Decimal = DEFAULT_OVERSTOCK_MULTIPLE,
    safety_stock_pct: Decimal | None = None,
    cover_days: int | None = None,
) -> list[CatalogScorecard]:
    """Run the harness for each `as_of` and return a list of scorecards."""
    return [
        score_at(
            db, as_of=d,
            overstock_multiple=overstock_multiple,
            safety_stock_pct=safety_stock_pct,
            cover_days=cover_days,
        )
        for d in as_of_dates
    ]


def last_n_month_starts(db: Session, n: int = 3) -> list[datetime]:
    """The last `n` calendar-month-starts present in the data, with at least
    60 days of pre-history for velocity. Each as_of is set to month-start
    midnight so the 60-day backward window is calendar-aligned."""
    row = db.execute(
        select(func.min(Order.placed_at), func.max(Order.placed_at))
    ).first()
    if not row or row[0] is None:
        return []
    first_at, last_at = row
    # Walk back from the LAST month-start in-data, skipping any that don't
    # have at least 60 days of pre-history.
    min_velocity_start = first_at + timedelta(days=60)

    out: list[datetime] = []
    # Anchor at the first day of last_at's month, then step back.
    y, m = last_at.year, last_at.month
    while len(out) < n and y > 1900:
        candidate = datetime(y, m, 1)
        if candidate >= min_velocity_start and candidate <= last_at:
            out.append(candidate)
        # Step back one month.
        if m == 1:
            y, m = y - 1, 12
        else:
            m -= 1
    return list(reversed(out))


# ---- CLI ----------------------------------------------------------------

def _format_money(d: Decimal) -> str:
    return f"${d:,.2f}"


def _format_scorecard(sc: CatalogScorecard, *, top_n: int = 5) -> str:
    """Pretty-print one scorecard for terminal output."""
    lines = []
    lines.append("")
    lines.append(f"=== Backtest scorecard: as_of = {sc.as_of} "
                 f"(actuals window = {sc.days_of_actuals_available} days of data available) ===")
    lines.append(f"  SKUs scored:                  {sc.skus_scored}")
    lines.append(f"  SKUs with recommendation:     {sc.skus_with_recommendation}")
    lines.append("")
    lines.append(f"  Stockout during lead time:    {sc.stockout_lead_count:>4} ({sc.stockout_lead_rate}% of all scored SKUs)")
    lines.append(f"  Stockout at 30d:              {sc.stockout_30d_count:>4} ({sc.stockout_30d_rate}% of SKUs with a PO)")
    lines.append(f"  Stockout at 60d:              {sc.stockout_60d_count:>4} ({sc.stockout_60d_rate}% of SKUs with a PO)")
    lines.append(f"  Overstock (qty > 2× 30d):     {sc.overstock_count:>4} ({sc.overstock_rate}% of SKUs with a PO)")
    lines.append("")
    lines.append(f"  Total recommended investment: {_format_money(sc.total_recommended_investment)}")
    lines.append(f"  Actual demand value 30d:      {_format_money(sc.total_actual_demand_value_30d)}")
    lines.append(f"  Actual demand value 60d:      {_format_money(sc.total_actual_demand_value_60d)}")
    lines.append(f"  Forecast efficiency 30d:      {sc.forecast_efficiency_30d}  "
                 f"(1.0 = matched; <1 = over-ordered; >1 = under-ordered)")

    stockout_offenders = sorted(
        (r for r in sc.per_sku if r.stockout_during_lead_time),
        key=lambda r: r.actual_demand_lead - r.on_hand_at_as_of,
        reverse=True,
    )[:top_n]
    if stockout_offenders:
        lines.append("")
        lines.append(f"  Top {len(stockout_offenders)} stockout offenders (during lead time):")
        lines.append(f"    {'sku':14} {'on_hand':>8} {'rec_qty':>8} {'lead_d':>8} {'gap':>8}")
        for r in stockout_offenders:
            gap = r.actual_demand_lead - r.on_hand_at_as_of
            lines.append(
                f"    {(r.sku_code or r.component_sku)[:14]:14} "
                f"{r.on_hand_at_as_of:>8} {r.recommended_qty:>8} "
                f"{r.actual_demand_lead:>8} {gap:>+8}"
            )

    overstock_offenders = sorted(
        (r for r in sc.per_sku if r.overstock and r.actual_demand_30d > 0),
        key=lambda r: r.recommended_qty / max(r.actual_demand_30d, 1),
        reverse=True,
    )[:top_n]
    if overstock_offenders:
        lines.append("")
        lines.append(f"  Top {len(overstock_offenders)} overstock offenders (rec >> 30d demand):")
        lines.append(f"    {'sku':14} {'rec_qty':>8} {'act_30d':>8} {'multiple':>10}")
        for r in overstock_offenders:
            mult = r.recommended_qty / max(r.actual_demand_30d, 1)
            lines.append(
                f"    {(r.sku_code or r.component_sku)[:14]:14} "
                f"{r.recommended_qty:>8} {r.actual_demand_30d:>8} {mult:>9.1f}x"
            )

    return "\n".join(lines)


def cli_main(argv: list[str] | None = None) -> int:
    import argparse
    from app.db import SessionLocal

    parser = argparse.ArgumentParser(
        prog="backtest",
        description="Replay the demand planner against historical dates.",
    )
    parser.add_argument(
        "dates", nargs="*",
        help="One or more as_of dates in YYYY-MM-DD format. "
             "When omitted, sweeps the last 3 month-starts in the data.",
    )
    parser.add_argument(
        "--top", type=int, default=5,
        help="How many top stockout/overstock offenders to list per scorecard (default 5).",
    )
    args = parser.parse_args(argv)

    with SessionLocal() as db:
        if args.dates:
            try:
                as_ofs = [datetime.fromisoformat(d) for d in args.dates]
            except ValueError as e:
                print(f"Bad date: {e}", flush=True)
                return 2
        else:
            as_ofs = last_n_month_starts(db, n=3)
            if not as_ofs:
                print("No historical month-starts with enough pre-history. "
                      "Pass explicit dates or load more orders.", flush=True)
                return 1

        scorecards = sweep(db, as_ofs)

    for sc in scorecards:
        print(_format_scorecard(sc, top_n=args.top))
    print("")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
