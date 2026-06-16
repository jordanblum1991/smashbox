"""Stockout history + lost-sales estimate from the daily inventory snapshots.

A snapshot at `on_hand == 0` is a stockout reading; with the daily sync, the
count of zero-readings in a window approximates the days a SKU was out. Lost
sales = days-out x the SKU's sales velocity — the demand that couldn't be
filled. A stockout only *costs* money when the SKU has demand: a SKU sitting at
zero with no velocity simply isn't carried, so its lost-sales estimate is 0.

Keyed by the SAP/SBX physical SKU code (same space as
app.services.demand.depletion, so velocity folds in via velocity_by_sap_sku).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.inventory_snapshot import InventorySnapshot


@dataclass
class StockoutStat:
    sap_sku: str
    stockout_readings: int       # captures at on_hand==0 in the window (≈ days out)
    total_readings: int          # captures on file in the window
    currently_out: bool          # latest reading in the window is 0
    last_out_at: datetime | None  # most recent zero-reading


def compute_stockout_stats(
    db: Session, *, window_days: int = 30, as_of: datetime | None = None,
) -> dict[str, StockoutStat]:
    """Per-SAP-SKU stockout readings over the trailing `window_days`. Pure
    snapshot signal — combine with sales velocity (see
    depletion.velocity_by_sap_sku) for a lost-units estimate."""
    from app.services.reporting_tz import now_local

    as_of = as_of or now_local()
    cutoff = as_of - timedelta(days=window_days)
    rows = db.execute(
        select(InventorySnapshot.sku, InventorySnapshot.captured_at, InventorySnapshot.on_hand)
        .where(InventorySnapshot.captured_at >= cutoff)
        .order_by(InventorySnapshot.captured_at)
    ).all()

    series: dict[str, list[tuple[datetime, int]]] = defaultdict(list)
    for sku, captured_at, on_hand in rows:
        series[sku].append((captured_at, int(on_hand or 0)))

    out: dict[str, StockoutStat] = {}
    for sku, pts in series.items():
        zeros = [t for t, h in pts if h == 0]
        if not zeros:
            continue
        out[sku] = StockoutStat(
            sap_sku=sku,
            stockout_readings=len(zeros),
            total_readings=len(pts),
            currently_out=(pts[-1][1] == 0),
            last_out_at=max(zeros),
        )
    return out


def estimate_lost_units(stat: StockoutStat | None, daily_sales) -> int:
    """Lost units ≈ stockout readings (≈ days out) x sales velocity. 0 when the
    SKU has no demand — an always-empty, never-sold SKU isn't a lost sale."""
    if stat is None or not stat.stockout_readings or daily_sales is None:
        return 0
    return int(round(float(stat.stockout_readings) * float(daily_sales)))
