"""Measured depletion rate from the daily inventory-snapshot time-series.

Consecutive on-hand readings give the units that actually LEFT the warehouse —
an independent demand signal that complements order-based velocity and exposes
movement not captured as TikTok sales (samples, breakage, shrinkage, other
channels). On-hand INCREASES are treated as receipts/restocks and excluded from
depletion (they're a separate signal).

Keyed by the SAP/SBX physical SKU code (`InventorySnapshot.sku`). Order velocity
is keyed by TikTok SKU ID; `velocity_by_sap_sku` folds it into the SBX space via
the `Sku` catalog (summing variations that share one physical code) so the two
can be compared like-for-like.

Validated on prod 2026-06-16: SAP depletion (116u, Jun 12-16) tracked TikTok
units sold (106u) — the warehouse depletes with FULFILLMENT_BY_SELLER orders.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.inventory_snapshot import InventorySnapshot
from app.models.sku import Sku


@dataclass
class DepletionStat:
    sap_sku: str              # SAP/SBX physical code (the snapshot key)
    snapshots: int            # readings in the window (need >=2 for a rate)
    first_at: datetime
    last_at: datetime
    span_days: int            # days between first and last reading
    units_depleted: int       # sum of on-hand DECREASES (receipts excluded)
    receipts: int             # sum of on-hand INCREASES (restocks)
    daily_depletion: Decimal  # units_depleted / span_days


@dataclass
class DepletionReconRow:
    sap_sku: str
    daily_depletion: Decimal  # measured, from snapshots
    daily_sales: Decimal      # order-velocity (60d) folded into the SBX space
    gap: Decimal              # depletion − sales; positive = unexplained outflow
    units_depleted: int
    span_days: int
    snapshots: int


def compute_depletion_rates(
    db: Session, *, window_days: int = 60, as_of: datetime | None = None,
) -> dict[str, DepletionStat]:
    """Per-SAP-SKU depletion stats over the trailing `window_days` of snapshots.
    Only SKUs with >=2 readings get a rate. Receipts (on-hand increases) are
    tallied separately and excluded from the depletion total."""
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

    out: dict[str, DepletionStat] = {}
    for sku, pts in series.items():
        if len(pts) < 2:
            continue
        pts.sort()
        depleted = receipts = 0
        for (_, h0), (_, h1) in zip(pts, pts[1:]):
            delta = h0 - h1
            if delta > 0:
                depleted += delta
            elif delta < 0:
                receipts += -delta
        span = (pts[-1][0] - pts[0][0]).days or 1
        out[sku] = DepletionStat(
            sap_sku=sku, snapshots=len(pts), first_at=pts[0][0], last_at=pts[-1][0],
            span_days=span, units_depleted=depleted, receipts=receipts,
            daily_depletion=(Decimal(depleted) / Decimal(span)).quantize(Decimal("0.01")),
        )
    return out


def velocity_by_sap_sku(db: Session, vel: dict) -> dict[str, Decimal]:
    """Fold order-velocity (keyed by TikTok SKU ID, incl. bundle components) into
    the SAP/SBX key space — summing any variations that share one physical code —
    so it lines up with `compute_depletion_rates`. A velocity key that isn't a
    catalog TikTok ID (already SBX-form) passes through unchanged."""
    ttid_to_sbx = {
        s.tiktok_sku_id: s.sku
        for s in db.execute(select(Sku)).scalars()
        if s.tiktok_sku_id and s.sku
    }
    out: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for key, v in vel.items():
        out[ttid_to_sbx.get(key, key)] += v.daily_60d
    return dict(out)


def reconcile_depletion_vs_sales(
    db: Session, *, window_days: int = 60, as_of: datetime | None = None,
    alias_map: dict[str, str] | None = None,
) -> list[DepletionReconRow]:
    """Per-SAP-SKU: measured depletion/day vs order-sales/day, sorted by the gap
    (largest unexplained outflow first). A persistent positive gap = inventory
    leaving faster than TikTok sales explain (samples / shrinkage / other
    channels); a negative gap = sales the warehouse feed hasn't caught up to."""
    from app.services.demand.velocity import compute_velocity
    from app.services.reporting_tz import now_local

    as_of = as_of or now_local()
    dep = compute_depletion_rates(db, window_days=window_days, as_of=as_of)
    vel = compute_velocity(db, as_of=as_of, alias_map=alias_map)
    sales_by_sbx = velocity_by_sap_sku(db, vel)

    rows = [
        DepletionReconRow(
            sap_sku=sku,
            daily_depletion=d.daily_depletion,
            daily_sales=sales_by_sbx.get(sku, Decimal("0")),
            gap=d.daily_depletion - sales_by_sbx.get(sku, Decimal("0")),
            units_depleted=d.units_depleted,
            span_days=d.span_days,
            snapshots=d.snapshots,
        )
        for sku, d in dep.items()
    ]
    rows.sort(key=lambda r: r.gap, reverse=True)
    return rows
