"""Demand planning report — for each component SKU, assemble:

  - Most recent on-hand from `InventorySnapshot`
  - Trailing 14d + 60d velocity from sales (bundle-expanded)
  - Procurement attrs from `Sku` (lead time, MOQ, case pack, etc.)
  - Replenishment math (`services/demand/replenishment.compute_one`)
  - Optional buyer-supplied "expected receipts" override

Returns a list of `ReplenishmentResult` rows sorted urgency-first plus a
period summary (investment over 30/60/90/180 days, counts by status, etc.).

Reads only — no writes. Buyer-side "expected receipts" overrides come in as
a `{component_sku: int}` dict (URL form params from the planner page); not
persisted in v1.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.inventory_snapshot import InventorySnapshot
from app.models.sku import Sku
from app.services.demand.replenishment import (
    STATUS_PRIORITY,
    ReplenishmentInputs,
    ReplenishmentResult,
    ReplenishmentStatus,
    compute_one,
)
from app.services.demand.velocity import compute_velocity


@dataclass
class DemandPlanningView:
    rows: list[ReplenishmentResult]
    as_of: date

    latest_snapshot_at: datetime | None
    snapshot_is_stale: bool       # True when latest snapshot is > 10 days old
    snapshot_age_days: int | None

    # The settings actually used to compute this view (after query-string
    # overrides). Surfaced so the UI can echo them back.
    safety_stock_pct: Decimal
    cover_days: int
    overstocked_days: int

    # Investment outlook over standard horizons.
    investment_total: Decimal     # equals sum of suggested_order_qty * unit_cogs
    investment_30d: Decimal
    investment_60d: Decimal
    investment_90d: Decimal
    investment_180d: Decimal

    @property
    def counts_by_status(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rows:
            out[r.status.value] = out.get(r.status.value, 0) + 1
        return out

    @property
    def reorder_now_rows(self) -> list[ReplenishmentResult]:
        return [r for r in self.rows if r.status == ReplenishmentStatus.REORDER_NOW]

    @property
    def at_risk_rows(self) -> list[ReplenishmentResult]:
        return [r for r in self.rows
                if r.status in (ReplenishmentStatus.OUT_OF_STOCK,
                                ReplenishmentStatus.AT_RISK)]

    @property
    def overstocked_rows(self) -> list[ReplenishmentResult]:
        return [r for r in self.rows if r.status == ReplenishmentStatus.OVERSTOCKED]


def _latest_on_hand_per_sku(db: Session) -> tuple[dict[str, int], datetime | None]:
    """Most-recent `on_hand` for every SKU we've ever snapshotted, plus the
    global latest `captured_at` (used for the "stale data" warning).
    """
    # Find the latest captured_at per SKU.
    latest_dt_per_sku = dict(
        db.execute(
            select(InventorySnapshot.sku, func.max(InventorySnapshot.captured_at))
            .group_by(InventorySnapshot.sku)
        ).all()
    )
    if not latest_dt_per_sku:
        return {}, None

    # Fetch the matching on_hand value.
    rows = db.execute(
        select(InventorySnapshot.sku, InventorySnapshot.on_hand, InventorySnapshot.captured_at)
        .where(InventorySnapshot.sku.in_(latest_dt_per_sku.keys()))
    ).all()
    on_hand: dict[str, int] = {}
    for sku, oh, captured_at in rows:
        if latest_dt_per_sku[sku] == captured_at:
            on_hand[sku] = int(oh or 0)

    global_latest = max(latest_dt_per_sku.values()) if latest_dt_per_sku else None
    return on_hand, global_latest


def _sku_by_component(db: Session, component_skus: set[str]) -> dict[str, Sku]:
    """Look up `Sku` rows by any of their identifiers (tiktok_sku_id,
    sku, tiktok_alt_sku). Returns one entry per component_sku that matched."""
    if not component_skus:
        return {}
    rows = db.execute(
        select(Sku).where(
            (Sku.tiktok_sku_id.in_(component_skus))
            | (Sku.sku.in_(component_skus))
            | (Sku.tiktok_alt_sku.in_(component_skus))
        )
    ).scalars().all()
    out: dict[str, Sku] = {}
    for s in rows:
        for key in (s.tiktok_sku_id, s.sku, s.tiktok_alt_sku):
            if key and key in component_skus:
                out[str(key)] = s
    return out


def compute_demand_planning_view(
    db: Session,
    *,
    safety_stock_pct: Decimal | None = None,
    cover_days: int | None = None,
    overstocked_days: int | None = None,
    expected_receipts: dict[str, int] | None = None,
    as_of: datetime | None = None,
) -> DemandPlanningView:
    """Build the full planner view.

    All tunables fall back to settings.py defaults when None. `expected_receipts`
    is a `{component_sku: in_transit_units}` dict supplied by the planner page
    (buyer overrides); not persisted.
    """
    safety = safety_stock_pct if safety_stock_pct is not None else settings.demand_safety_stock_pct
    cover = cover_days if cover_days is not None else settings.demand_cover_days
    overstocked = overstocked_days if overstocked_days is not None else settings.demand_overstocked_days
    now = as_of or datetime.now()
    expected_receipts = expected_receipts or {}

    # Velocity per component SKU (bundle-expanded).
    velocities = compute_velocity(db, as_of=now)

    # On-hand per inventory snapshot SKU.
    on_hand_by_sku, latest_snapshot_at = _latest_on_hand_per_sku(db)

    # Union of all SKUs we have ANY signal for. Skip empties.
    all_skus = set(velocities) | set(on_hand_by_sku)
    if not all_skus:
        # No data yet — return empty view.
        return DemandPlanningView(
            rows=[], as_of=now.date(),
            latest_snapshot_at=None,
            snapshot_is_stale=False,
            snapshot_age_days=None,
            safety_stock_pct=safety, cover_days=cover, overstocked_days=overstocked,
            investment_total=Decimal("0"),
            investment_30d=Decimal("0"), investment_60d=Decimal("0"),
            investment_90d=Decimal("0"), investment_180d=Decimal("0"),
        )

    sku_meta = _sku_by_component(db, all_skus)

    # Build replenishment inputs + compute per SKU.
    results: list[ReplenishmentResult] = []
    for component_sku in all_skus:
        v = velocities.get(component_sku)
        s = sku_meta.get(component_sku)

        on_hand = on_hand_by_sku.get(component_sku, 0)
        receipts = int(expected_receipts.get(component_sku, 0))

        lead_time = (s.lead_time_days if s and s.lead_time_days else
                     settings.demand_lead_time_default_days)
        moq = (s.moq or 0) if s else 0
        case_pack = (s.case_pack or 0) if s else 0
        # `safety_stock_pct` on Sku is currently stored as a "%" number
        # (e.g. 25.00 means 25%) — divide. Falls back to global default when null.
        sku_safety_pct = None
        if s and s.safety_stock_pct is not None:
            try:
                sku_safety_pct = Decimal(str(s.safety_stock_pct)) / Decimal("100")
            except Exception:  # noqa: BLE001
                sku_safety_pct = None
        effective_safety = sku_safety_pct if sku_safety_pct is not None else safety

        is_reorderable = True if not s else (s.is_reorderable if s.is_reorderable is not None else True)
        unit_cogs = Decimal(str(s.unit_cogs)) if (s and s.unit_cogs) else Decimal("0")

        inputs = ReplenishmentInputs(
            sku_code=(s.sku if s else None),
            component_sku=component_sku,
            name=(s.name if s else None),
            on_hand=on_hand,
            expected_receipts=receipts,
            daily_velocity=v.daily_60d if v else Decimal("0"),
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
        results.append(compute_one(inputs, as_of=now.date()))

    # Sort: urgency first, then highest investment first within group.
    results.sort(key=lambda r: (STATUS_PRIORITY[r.status], -float(r.investment)))

    # Stale-snapshot detection.
    snapshot_age_days = None
    snapshot_is_stale = False
    if latest_snapshot_at:
        snapshot_age_days = (now - latest_snapshot_at).days
        snapshot_is_stale = snapshot_age_days > 10  # ~weekly cadence + 3-day grace

    # Investment outlooks. Suggested-order-qty already covers (lead_time + cover_days)
    # for each reorder candidate; for the broader horizons, we project velocity × days
    # for every SKU that will need *some* PO at all in the window.
    investment_total = sum((r.investment for r in results), Decimal("0"))

    def _investment_window(days: int) -> Decimal:
        total = Decimal("0")
        for r in results:
            if r.status in (ReplenishmentStatus.HEALTHY,
                            ReplenishmentStatus.OVERSTOCKED,
                            ReplenishmentStatus.DISCONTINUED,
                            ReplenishmentStatus.NO_VELOCITY):
                continue
            sku = sku_meta.get(r.component_sku)
            unit_cogs = Decimal(str(sku.unit_cogs)) if (sku and sku.unit_cogs) else Decimal("0")
            # Project: units needed over `days` ahead, minus what's already in
            # the immediate suggested PO. The two together give cumulative spend.
            window_demand = Decimal(days) * r.daily_velocity
            shortfall = window_demand - Decimal(r.available)
            if shortfall <= 0:
                continue
            total += (shortfall * unit_cogs).quantize(Decimal("0.01"))
        return total

    return DemandPlanningView(
        rows=results, as_of=now.date(),
        latest_snapshot_at=latest_snapshot_at,
        snapshot_is_stale=snapshot_is_stale,
        snapshot_age_days=snapshot_age_days,
        safety_stock_pct=safety, cover_days=cover, overstocked_days=overstocked,
        investment_total=investment_total,
        investment_30d=_investment_window(30),
        investment_60d=_investment_window(60),
        investment_90d=_investment_window(90),
        investment_180d=_investment_window(180),
    )
