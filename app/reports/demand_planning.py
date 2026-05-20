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
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.bundle import Bundle, BundleComponent
from app.models.inventory_snapshot import InventorySnapshot
from app.models.order import Order, OrderLine, OrderType
from app.models.sku import Sku
from app.services.demand.bundle_expansion import bundle_component_breakdown
from app.services.demand.replenishment import (
    STATUS_PRIORITY,
    ReplenishmentInputs,
    ReplenishmentResult,
    ReplenishmentStatus,
    compute_one,
)
from app.services.demand.velocity import COUNTED_STATUSES, compute_velocity


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


# =============================================================================
# Per-SKU drill-down
# =============================================================================

@dataclass
class WeeklyVelocityBucket:
    """One bar in the weekly velocity chart. `week_start` is Monday of the week."""
    week_start: date
    units: int


@dataclass
class InventorySnapshotRow:
    """One row in the inventory history table."""
    captured_at: datetime
    on_hand: int


@dataclass
class BundleRelationship:
    """Either a bundle this SKU is a component of, OR — if this SKU is itself
    a bundle — the components inside it."""
    bundle_sku: str | None
    tiktok_sku_id: str | None
    name: str
    qty: int   # qty of THIS sku inside the bundle


@dataclass
class SkuDemandBreakdown:
    """Sixty-day order-line mix for one SKU, broken out by what the planner
    counts vs what it filters out. Buyer uses this to reconcile our planner
    counts against TikTok Seller Center's raw line view. Totals match
    `OrderLine` rows directly — no bundle expansion (intentional: TikTok's
    view is at the order-line level)."""
    counted_units: int        # PAID/PAID_SAMPLE + Shipped/Completed — drives velocity
    counted_lines: int
    free_sample_units: int    # type=SAMPLE + Shipped/Completed — see Sample Tracking report
    free_sample_lines: int
    canceled_units: int       # any type, status=Canceled
    canceled_lines: int
    pending_units: int        # any type, status="To ship" (fulfillment not yet underway)
    pending_lines: int
    other_units: int          # any other status (Failed, Withdrawn, etc.)
    other_lines: int

    @property
    def total_units(self) -> int:
        return (self.counted_units + self.free_sample_units
                + self.canceled_units + self.pending_units + self.other_units)

    @property
    def total_lines(self) -> int:
        return (self.counted_lines + self.free_sample_lines
                + self.canceled_lines + self.pending_lines + self.other_lines)


@dataclass
class SkuDetailView:
    # The replenishment row (same shape as on the main table).
    row: ReplenishmentResult

    # Settings actually used.
    safety_stock_pct: Decimal
    cover_days: int

    # Source data.
    sku: Sku | None
    weekly_velocity: list[WeeklyVelocityBucket]  # last 12 weeks
    inventory_history: list[InventorySnapshotRow]  # all snapshots, newest first
    bundle_parents: list[BundleRelationship]      # bundles this SKU is INSIDE
    bundle_components: list[BundleRelationship]   # if THIS SKU is a bundle, its parts

    # Math walkthrough — pre-computed so the template can render the formula clearly.
    lead_time_demand: int       # velocity × lead_time_days
    safety_buffer: int          # lead_time_demand × safety_pct
    target_units: int           # velocity × (lead_time + cover_days)
    available: int              # on_hand + expected_receipts

    # Global defaults — shown as placeholders in the procurement editor so the
    # buyer knows what value applies when a per-SKU override is blank.
    default_lead_time_days: int
    default_safety_stock_pct: Decimal

    # Order-line mix over 60 days — for cross-checking against TikTok.
    demand_breakdown: "SkuDemandBreakdown | None" = None


def _weekly_velocity(
    db: Session, component_sku: str, *, as_of: datetime, weeks: int = 12
) -> list[WeeklyVelocityBucket]:
    """Bundle-expanded units shipped per ISO week for the trailing `weeks` weeks.

    Walks back from this week's Monday. Returns weeks oldest-first so the
    chart reads left-to-right chronologically.
    """
    # Snap `as_of` back to the current week's Monday (ISO day-of-week = 1).
    monday = (as_of - timedelta(days=as_of.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start = monday - timedelta(weeks=weeks - 1)
    end = monday + timedelta(weeks=1)

    # Raw `{order_line.sku: units_per_week}` — we need per-week bucketing.
    # SQLite doesn't have date_trunc, but func.date() can be combined with
    # arithmetic. Cleanest portable approach: fetch raw rows, bucket in Python.
    rows = db.execute(
        select(OrderLine.sku, OrderLine.quantity, Order.placed_at)
        .join(Order, Order.id == OrderLine.order_id)
        .where(Order.placed_at >= start, Order.placed_at < end)
        .where(Order.order_type.in_([OrderType.PAID, OrderType.PAID_SAMPLE]))
        .where(Order.status.in_(COUNTED_STATUSES))
    ).all()

    # Build {order_sku: {week_start: units}} so bundle expansion runs once.
    sku_week_units: dict[str, dict[date, int]] = {}
    for sku, qty, placed_at in rows:
        wk_start = (placed_at - timedelta(days=placed_at.weekday())).date()
        sku_week_units.setdefault(sku, {}).setdefault(wk_start, 0)
        sku_week_units[sku][wk_start] += int(qty or 0)

    # Bundle expansion: for each order SKU, map its weekly units to the
    # component_sku we care about (might be the same SKU, or one of the
    # components inside a bundle).
    bundle_map = bundle_component_breakdown(db, set(sku_week_units))

    component_weekly: dict[date, int] = {}
    for order_sku, week_units in sku_week_units.items():
        components = bundle_map.get(order_sku)
        if components is None:
            # Non-bundle: only matters if this IS the component we want.
            if order_sku != component_sku:
                continue
            for wk, n in week_units.items():
                component_weekly[wk] = component_weekly.get(wk, 0) + n
        else:
            # Bundle: pick out our component, if any.
            for cs, qty_per in components:
                if cs != component_sku:
                    continue
                for wk, n in week_units.items():
                    component_weekly[wk] = component_weekly.get(wk, 0) + n * qty_per

    # Render every week in the window, even if zero, so the chart has uniform bars.
    out = []
    for i in range(weeks):
        wk_start = (monday - timedelta(weeks=weeks - 1 - i)).date()
        out.append(WeeklyVelocityBucket(week_start=wk_start,
                                        units=component_weekly.get(wk_start, 0)))
    return out


def _demand_breakdown(
    db: Session, component_sku: str, *, as_of: datetime
) -> SkuDemandBreakdown:
    """60-day order-line mix for `component_sku`. Buckets every line by what
    the planner does with it: counted as demand, treated as a free sample,
    cancelled, pending, or other. Useful for cross-checking against TikTok
    Seller Center's raw line view."""
    end_date = as_of.date()
    start_date = end_date - timedelta(days=60)
    start_dt = datetime(start_date.year, start_date.month, start_date.day)
    end_dt = datetime(end_date.year, end_date.month, end_date.day)

    rows = db.execute(
        select(Order.status, Order.order_type,
               func.count(OrderLine.id),
               func.coalesce(func.sum(OrderLine.quantity), 0))
        .join(Order, Order.id == OrderLine.order_id)
        .where(OrderLine.sku == component_sku)
        .where(Order.placed_at >= start_dt, Order.placed_at < end_dt)
        .group_by(Order.status, Order.order_type)
    ).all()

    counted_u = counted_l = 0
    sample_u = sample_l = 0
    canceled_u = canceled_l = 0
    pending_u = pending_l = 0
    other_u = other_l = 0

    for status, otype, n_lines, units in rows:
        u = int(units or 0)
        ln = int(n_lines or 0)
        if otype in (OrderType.PAID, OrderType.PAID_SAMPLE) and status in COUNTED_STATUSES:
            counted_u += u
            counted_l += ln
        elif otype == OrderType.SAMPLE and status in COUNTED_STATUSES:
            sample_u += u
            sample_l += ln
        elif status == "Canceled":
            canceled_u += u
            canceled_l += ln
        elif status == "To ship":
            pending_u += u
            pending_l += ln
        else:
            other_u += u
            other_l += ln

    return SkuDemandBreakdown(
        counted_units=counted_u, counted_lines=counted_l,
        free_sample_units=sample_u, free_sample_lines=sample_l,
        canceled_units=canceled_u, canceled_lines=canceled_l,
        pending_units=pending_u, pending_lines=pending_l,
        other_units=other_u, other_lines=other_l,
    )


def _inventory_history(db: Session, component_sku: str) -> list[InventorySnapshotRow]:
    """All snapshots for this SKU, newest first. Used for the drill-down's
    "Inventory history" table — usually <= a few rows at the current cadence."""
    rows = db.execute(
        select(InventorySnapshot.captured_at, InventorySnapshot.on_hand)
        .where(InventorySnapshot.sku == component_sku)
        .order_by(InventorySnapshot.captured_at.desc())
    ).all()
    return [InventorySnapshotRow(captured_at=ca, on_hand=int(oh or 0))
            for ca, oh in rows]


def _bundle_relationships(
    db: Session, sku_obj: Sku | None, component_sku: str
) -> tuple[list[BundleRelationship], list[BundleRelationship]]:
    """Find:
       (a) bundles this SKU is a component INSIDE of (parents)
       (b) if this component_sku is itself a bundle key, its child components.
    """
    parents: list[BundleRelationship] = []
    children: list[BundleRelationship] = []

    # Parents: BundleComponent rows where component_sku matches this SKU
    # by any of its identifiers (SBX form, TikTok SKU ID, alt SKU).
    candidate_keys: set[str] = {component_sku}
    if sku_obj:
        for k in (sku_obj.sku, sku_obj.tiktok_sku_id, sku_obj.tiktok_alt_sku):
            if k:
                candidate_keys.add(str(k))

    component_rows = db.execute(
        select(BundleComponent).where(BundleComponent.component_sku.in_(candidate_keys))
    ).scalars().all()
    if component_rows:
        bundle_ids = {c.bundle_id for c in component_rows}
        bundles_by_id = {
            b.id: b for b in db.execute(
                select(Bundle).where(Bundle.id.in_(bundle_ids))
            ).scalars()
        }
        for c in component_rows:
            b = bundles_by_id.get(c.bundle_id)
            if not b:
                continue
            parents.append(BundleRelationship(
                bundle_sku=b.bundle_sku,
                tiktok_sku_id=b.tiktok_sku_id,
                name=b.name or "—",
                qty=int(c.quantity or 0),
            ))

    # Children: if this SKU is itself a bundle, list its components.
    bundle = db.execute(
        select(Bundle).where(
            (Bundle.tiktok_sku_id == component_sku)
            | (Bundle.bundle_sku == component_sku)
        )
    ).scalar_one_or_none()
    if bundle:
        for c in db.execute(
            select(BundleComponent).where(BundleComponent.bundle_id == bundle.id)
        ).scalars():
            children.append(BundleRelationship(
                bundle_sku=c.component_sku,
                tiktok_sku_id=None,
                name=c.component_name or "—",
                qty=int(c.quantity or 0),
            ))

    return parents, children


def compute_sku_detail_view(
    db: Session,
    component_sku: str,
    *,
    safety_stock_pct: Decimal | None = None,
    cover_days: int | None = None,
    overstocked_days: int | None = None,
    expected_receipts: int = 0,
    as_of: datetime | None = None,
) -> SkuDetailView | None:
    """Drill-down for one component SKU. Returns None when the SKU has no
    velocity AND no inventory snapshot — i.e. we have no data on it at all."""
    safety = safety_stock_pct if safety_stock_pct is not None else settings.demand_safety_stock_pct
    cover = cover_days if cover_days is not None else settings.demand_cover_days
    overstocked = overstocked_days if overstocked_days is not None else settings.demand_overstocked_days
    now = as_of or datetime.now()

    # Pull just this SKU's signals.
    velocities = compute_velocity(db, as_of=now)
    v = velocities.get(component_sku)

    on_hand_rows = db.execute(
        select(InventorySnapshot.captured_at, InventorySnapshot.on_hand)
        .where(InventorySnapshot.sku == component_sku)
        .order_by(InventorySnapshot.captured_at.desc())
        .limit(1)
    ).first()
    on_hand = int(on_hand_rows.on_hand) if on_hand_rows else 0

    if v is None and not on_hand_rows:
        return None

    sku = _sku_by_component(db, {component_sku}).get(component_sku)

    # Effective procurement attrs.
    lead_time = (sku.lead_time_days if sku and sku.lead_time_days
                 else settings.demand_lead_time_default_days)
    moq = (sku.moq or 0) if sku else 0
    case_pack = (sku.case_pack or 0) if sku else 0
    sku_safety_pct = None
    if sku and sku.safety_stock_pct is not None:
        try:
            sku_safety_pct = Decimal(str(sku.safety_stock_pct)) / Decimal("100")
        except Exception:  # noqa: BLE001
            sku_safety_pct = None
    effective_safety = sku_safety_pct if sku_safety_pct is not None else safety
    is_reorderable = True if not sku else (
        sku.is_reorderable if sku.is_reorderable is not None else True
    )
    unit_cogs = Decimal(str(sku.unit_cogs)) if (sku and sku.unit_cogs) else Decimal("0")

    inputs = ReplenishmentInputs(
        sku_code=(sku.sku if sku else None),
        component_sku=component_sku,
        name=(sku.name if sku else None),
        on_hand=on_hand,
        expected_receipts=expected_receipts,
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
    row = compute_one(inputs, as_of=now.date())

    # Math breakdown — recompute the intermediate values for display.
    lead_time_demand = int((inputs.daily_velocity * Decimal(inputs.lead_time_days))
                           .to_integral_value(rounding="ROUND_HALF_UP"))
    safety_buffer = int((Decimal(lead_time_demand) * inputs.safety_stock_pct)
                        .to_integral_value(rounding="ROUND_HALF_UP"))
    target_units = int((inputs.daily_velocity * Decimal(inputs.lead_time_days + inputs.cover_days))
                       .to_integral_value(rounding="ROUND_HALF_UP"))
    available = on_hand + expected_receipts

    parents, children = _bundle_relationships(db, sku, component_sku)

    return SkuDetailView(
        row=row,
        safety_stock_pct=effective_safety,
        cover_days=cover,
        sku=sku,
        weekly_velocity=_weekly_velocity(db, component_sku, as_of=now),
        inventory_history=_inventory_history(db, component_sku),
        bundle_parents=parents,
        bundle_components=children,
        lead_time_demand=lead_time_demand,
        safety_buffer=safety_buffer,
        target_units=target_units,
        available=available,
        default_lead_time_days=settings.demand_lead_time_default_days,
        default_safety_stock_pct=settings.demand_safety_stock_pct,
        demand_breakdown=_demand_breakdown(db, component_sku, as_of=now),
    )
