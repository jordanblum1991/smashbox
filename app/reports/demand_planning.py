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
class PipelineItem:
    """One projected purchase order in the next-90-day pipeline.

    For SKUs already at/below reorder point (out_of_stock, at_risk,
    reorder_now): order_by_date is today (or earlier), qty/investment
    come straight from compute_one.

    For SKUs currently healthy/overstocked: we project when on_hand will
    cross reorder_point at current velocity, and compute the order
    quantity as if we caught the SKU exactly at the crossing
    (target = v × (lead_time + cover_days), available = reorder_point)."""
    sku_code: str | None
    component_sku: str
    name: str | None
    status: ReplenishmentStatus
    on_hand: int
    in_transit: int
    daily_velocity: Decimal
    lead_time_days: int
    reorder_point: int
    days_until_reorder: int
    order_by_date: date
    suggested_qty: int
    investment: Decimal


@dataclass
class PurchasePipeline:
    """Bucketed forward-looking PO calendar. Buckets are mutually exclusive:
    each PipelineItem appears in exactly one of overdue / next_30 / next_60 / next_90."""
    overdue: list[PipelineItem] = field(default_factory=list)
    next_30: list[PipelineItem] = field(default_factory=list)
    next_60: list[PipelineItem] = field(default_factory=list)
    next_90: list[PipelineItem] = field(default_factory=list)

    @property
    def overdue_investment(self) -> Decimal:
        return sum((i.investment for i in self.overdue), Decimal("0"))

    @property
    def next_30_investment(self) -> Decimal:
        return sum((i.investment for i in self.next_30), Decimal("0"))

    @property
    def next_60_investment(self) -> Decimal:
        return sum((i.investment for i in self.next_60), Decimal("0"))

    @property
    def next_90_investment(self) -> Decimal:
        return sum((i.investment for i in self.next_90), Decimal("0"))

    @property
    def total_investment(self) -> Decimal:
        return (self.overdue_investment + self.next_30_investment
                + self.next_60_investment + self.next_90_investment)

    @property
    def all_items_sorted(self) -> list[PipelineItem]:
        """Flat list across all buckets, sorted by order_by_date ascending."""
        flat = self.overdue + self.next_30 + self.next_60 + self.next_90
        return sorted(flat, key=lambda i: (i.order_by_date, -float(i.investment)))


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

    # Forward-looking PO calendar — see compute_purchase_pipeline.
    pipeline: PurchasePipeline = field(default_factory=PurchasePipeline)

    # Effective global service level for this view (after page-level override).
    # Surfaced so the Service Level dropdown can echo the active setting.
    service_level: Decimal = Decimal("0.95")

    @property
    def snapshot_freshness_state(self) -> str:
        """Day-bucket for the snapshot-age banner on the planner.

        Mapping (per the dashboard reminder spec):
          None        → 'missing'   No snapshot ever uploaded.
          15+ days    → 'stale'     Reorder math unreliable.
          8–14 days   → 'aging'     Upload soon.
          0–7 days    → 'fresh'     Subtle confirmation.

        The legacy `snapshot_is_stale` field (>10-day threshold) is kept
        for backward compat with any other readers but the planner banner
        now keys off this property's four-tier state.
        """
        if self.snapshot_age_days is None:
            return "missing"
        if self.snapshot_age_days >= 15:
            return "stale"
        if self.snapshot_age_days >= 8:
            return "aging"
        return "fresh"

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


def _latest_on_hand_per_sku(
    db: Session, *, alias_map: dict[str, str] | None = None,
) -> tuple[dict[str, int], datetime | None]:
    """Most-recent `on_hand` for every SKU we've ever snapshotted, plus the
    global latest `captured_at` (used for the "stale data" warning).

    When `alias_map` is provided, aliased SKUs collapse to their canonical:
    among all snapshots whose `sku` (alias or canonical) point to the same
    canonical, the most-recently-captured one wins. This matches the
    "re-coded product" model — the new code's latest snapshot reflects the
    physical truth; the old code's snapshot is stale by definition."""
    # Find the latest captured_at per raw SKU (alias collapse happens below).
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
    latest_per_canonical: dict[str, datetime] = {}
    alias_map = alias_map or {}
    for sku, oh, captured_at in rows:
        if latest_dt_per_sku[sku] != captured_at:
            continue
        canonical = alias_map.get(sku, sku)
        prev = latest_per_canonical.get(canonical)
        if prev is None or captured_at > prev:
            latest_per_canonical[canonical] = captured_at
            on_hand[canonical] = int(oh or 0)

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
    service_level_override: Decimal | None = None,
    cover_days: int | None = None,
    overstocked_days: int | None = None,
    expected_receipts: dict[str, int] | None = None,
    as_of: datetime | None = None,
) -> DemandPlanningView:
    """Build the full planner view.

    All tunables fall back to settings.py defaults when None. `expected_receipts`
    is a `{component_sku: in_transit_units}` dict supplied by the planner page
    (buyer overrides); not persisted.

    `service_level_override`, when set, overrides the global default service
    level for the variance-based safety stock z lookup. Per-SKU
    `Sku.service_level` still wins above this override for individual SKUs.
    Must be one of the three supported tiers (0.90 / 0.95 / 0.975);
    otherwise the global default applies.
    """
    safety = safety_stock_pct if safety_stock_pct is not None else settings.demand_safety_stock_pct
    cover = cover_days if cover_days is not None else settings.demand_cover_days
    overstocked = overstocked_days if overstocked_days is not None else settings.demand_overstocked_days
    effective_global_service_level = (service_level_override
                                       if service_level_override is not None
                                       else settings.demand_service_level_default)
    now = as_of or datetime.now()
    expected_receipts = expected_receipts or {}

    # Load the alias map once and thread it through both signals so a
    # re-coded SKU's demand AND on-hand history collapse into one signal.
    from app.services.sku_alias import load_alias_map
    alias_map = load_alias_map(db)

    # Cold-start detection: when did each component first sell? Threaded
    # into compute_velocity so SkuVelocity.days_observed reflects "days since
    # first sale" (clamped to WINDOW_DAYS) instead of the full 60.
    from app.services.demand.velocity import compute_first_sold_at_per_component
    first_sold_at = compute_first_sold_at_per_component(db, alias_map=alias_map)

    # Velocity per component SKU (bundle-expanded, alias-collapsed).
    velocities = compute_velocity(
        db, as_of=now, alias_map=alias_map, first_sold_at=first_sold_at,
    )

    # On-hand per inventory snapshot SKU (alias-collapsed to canonical).
    on_hand_by_sku, latest_snapshot_at = _latest_on_hand_per_sku(db, alias_map=alias_map)

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
            service_level=effective_global_service_level,
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

        # Variance-based safety stock inputs: σ from the RAW daily series
        # (NOT the spike-capped one — capping shrinks σ and under-buffers
        # the very volatility we're insuring against). Service level falls
        # back to the global default when the per-SKU value is null.
        from app.config import z_for_service_level
        sigma_daily = v.sigma_daily_raw if v else None
        sku_service_level = s.service_level if (s and s.service_level is not None) else None
        # Per-SKU service_level wins over the page-level override; the page-level
        # override wins over settings.demand_service_level_default.
        effective_service_level = sku_service_level or effective_global_service_level
        try:
            z_value = z_for_service_level(Decimal(str(effective_service_level)))
        except KeyError:
            # Unsupported per-SKU service level — fall back to page-level
            # (which is itself the global default when no override is set).
            z_value = z_for_service_level(effective_global_service_level)

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
            sigma_daily=sigma_daily,
            z_value=z_value,
            service_level=Decimal(str(effective_service_level)) if effective_service_level is not None else None,
            cover_days=cover,
            overstocked_threshold_days=overstocked,
            moq=moq,
            case_pack=case_pack,
            is_reorderable=is_reorderable,
            unit_cogs=unit_cogs,
            days_observed=v.days_observed if v else 60,
            units_observed=v.units_60d if v else None,
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

    pipeline = compute_purchase_pipeline(
        results, sku_meta, today=now.date(), cover_days=cover,
    )

    return DemandPlanningView(
        rows=results, as_of=now.date(),
        latest_snapshot_at=latest_snapshot_at,
        snapshot_is_stale=snapshot_is_stale,
        snapshot_age_days=snapshot_age_days,
        safety_stock_pct=safety, cover_days=cover, overstocked_days=overstocked,
        service_level=effective_global_service_level,
        investment_total=investment_total,
        investment_30d=_investment_window(30),
        investment_60d=_investment_window(60),
        investment_90d=_investment_window(90),
        investment_180d=_investment_window(180),
        pipeline=pipeline,
    )


def compute_purchase_pipeline(
    results: list[ReplenishmentResult],
    sku_meta: dict[str, Sku],
    *,
    today: date,
    cover_days: int,
) -> PurchasePipeline:
    """Forward-looking PO calendar for the next 90 days.

    For each reorderable SKU with non-zero velocity, project the date at
    which on_hand will cross the reorder point. SKUs already at/below the
    reorder point land in `overdue`; everything else buckets into 30/60/90
    by `days_until_reorder`. SKUs whose projected crossing is >90 days out
    are excluded (they're shown on the regular planner table; they don't
    need pipeline visibility yet).

    Quantity logic:
      - status is OUT_OF_STOCK / AT_RISK / REORDER_NOW → use compute_one's
        existing suggested_order_qty + investment (already MOQ/case-pack
        adjusted, already reflects current on_hand).
      - status is HEALTHY / OVERSTOCKED → project: PO must cover
        (lead_time + cover_days) of forward demand starting from
        on_hand = reorder_point, so target = v × (lead+cover); quantity
        = target − reorder_point. Doesn't apply MOQ/case-pack rounding
        because we don't know the SKU's procurement attrs from
        ReplenishmentResult — close enough for a forecast.
    """
    pipeline = PurchasePipeline()
    horizon_days = 90

    for r in results:
        if r.status in (ReplenishmentStatus.DISCONTINUED,
                        ReplenishmentStatus.NO_VELOCITY):
            continue
        if r.daily_velocity <= 0:
            continue

        v = r.daily_velocity
        available_now = Decimal(r.on_hand + r.expected_receipts)
        reorder_pt = Decimal(r.reorder_point)

        # Days until on_hand drops to the reorder point at current velocity.
        if available_now <= reorder_pt:
            days_until = 0
        else:
            days_until = int(((available_now - reorder_pt) / v)
                             .to_integral_value(rounding="ROUND_DOWN"))

        if days_until > horizon_days:
            continue   # outside the 90-day pipeline view

        order_by_date = today + timedelta(days=days_until)

        # Quantity + investment.
        if r.suggested_order_qty > 0:
            qty = r.suggested_order_qty
            investment = r.investment
        else:
            target = v * Decimal(r.lead_time_days + cover_days)
            raw_qty = (target - reorder_pt).to_integral_value(rounding="ROUND_HALF_UP")
            qty = max(int(raw_qty), 0)
            sku = sku_meta.get(r.component_sku)
            unit_cogs = (Decimal(str(sku.unit_cogs))
                         if (sku and sku.unit_cogs) else Decimal("0"))
            investment = (Decimal(qty) * unit_cogs).quantize(Decimal("0.01"))

        item = PipelineItem(
            sku_code=r.sku_code,
            component_sku=r.component_sku,
            name=r.name,
            status=r.status,
            on_hand=r.on_hand,
            in_transit=r.expected_receipts,
            daily_velocity=v,
            lead_time_days=r.lead_time_days,
            reorder_point=r.reorder_point,
            days_until_reorder=days_until,
            order_by_date=order_by_date,
            suggested_qty=qty,
            investment=investment,
        )

        if days_until <= 0:
            pipeline.overdue.append(item)
        elif days_until <= 30:
            pipeline.next_30.append(item)
        elif days_until <= 60:
            pipeline.next_60.append(item)
        else:
            pipeline.next_90.append(item)

    # Within each bucket, soonest first; tie-break by larger investment.
    for bucket in (pipeline.overdue, pipeline.next_30,
                   pipeline.next_60, pipeline.next_90):
        bucket.sort(key=lambda i: (i.order_by_date, -float(i.investment)))

    return pipeline


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
    db: Session, component_sku: str, *, as_of: datetime, weeks: int = 12,
    alias_map: dict[str, str] | None = None,
) -> list[WeeklyVelocityBucket]:
    """Bundle-expanded units shipped per ISO week for the trailing `weeks` weeks.

    Walks back from this week's Monday. Returns weeks oldest-first so the
    chart reads left-to-right chronologically.

    `alias_map` rolls aliased SKUs into their canonical at the order_sku
    AND component_sku levels — so a legacy code's pre-rename sales show
    up under the canonical SKU's chart.
    """
    alias_map = alias_map or {}
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
    # Alias-collapse the order_sku as we bucket so a re-coded product's pre-
    # and post-rename weeks merge cleanly.
    sku_week_units: dict[str, dict[date, int]] = {}
    for sku, qty, placed_at in rows:
        canonical_order_sku = alias_map.get(sku, sku)
        wk_start = (placed_at - timedelta(days=placed_at.weekday())).date()
        sku_week_units.setdefault(canonical_order_sku, {}).setdefault(wk_start, 0)
        sku_week_units[canonical_order_sku][wk_start] += int(qty or 0)

    # Bundle expansion: for each order SKU, map its weekly units to the
    # component_sku we care about (might be the same SKU, or one of the
    # components inside a bundle). Apply alias_map to the component side too.
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
                if alias_map.get(cs, cs) != component_sku:
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
    db: Session, component_sku: str, *, as_of: datetime,
    alias_map: dict[str, str] | None = None,
) -> SkuDemandBreakdown:
    """60-day order-line mix for `component_sku`. Buckets every line by what
    the planner does with it: counted as demand, treated as a free sample,
    cancelled, pending, or other. Useful for cross-checking against TikTok
    Seller Center's raw line view.

    When `alias_map` is supplied, aliases of `component_sku` are included
    so a re-coded product's old-code lines show up in the canonical's mix."""
    alias_map = alias_map or {}
    end_date = as_of.date()
    start_date = end_date - timedelta(days=60)
    start_dt = datetime(start_date.year, start_date.month, start_date.day)
    end_dt = datetime(end_date.year, end_date.month, end_date.day)

    aliases_of = {component_sku} | {a for a, c in alias_map.items() if c == component_sku}
    rows = db.execute(
        select(Order.status, Order.order_type,
               func.count(OrderLine.id),
               func.coalesce(func.sum(OrderLine.quantity), 0))
        .join(Order, Order.id == OrderLine.order_id)
        .where(OrderLine.sku.in_(aliases_of))
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


def _inventory_history(
    db: Session, component_sku: str,
    *, alias_map: dict[str, str] | None = None,
) -> list[InventorySnapshotRow]:
    """All snapshots for this SKU, newest first. Used for the drill-down's
    "Inventory history" table — usually <= a few rows at the current cadence.

    Includes aliases of the canonical SKU when `alias_map` is provided so
    snapshots taken under the legacy code show in the canonical's history."""
    alias_map = alias_map or {}
    aliases_of = {component_sku} | {a for a, c in alias_map.items() if c == component_sku}
    rows = db.execute(
        select(InventorySnapshot.captured_at, InventorySnapshot.on_hand)
        .where(InventorySnapshot.sku.in_(aliases_of))
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

    # Load the alias map and resolve the requested SKU to its canonical
    # form — drilling into a legacy code should land on the canonical SKU's
    # combined history, not a half-empty view of the pre-rename window.
    from app.services.sku_alias import load_alias_map
    alias_map = load_alias_map(db)
    component_sku = alias_map.get(component_sku, component_sku)

    velocities = compute_velocity(db, as_of=now, alias_map=alias_map)
    v = velocities.get(component_sku)

    # Inventory: pull every snapshot whose alias collapses to this canonical,
    # then take the most-recent one (latest captured_at wins among aliases).
    aliases_of_canonical = {component_sku} | {
        a for a, c in alias_map.items() if c == component_sku
    }
    on_hand_rows = db.execute(
        select(InventorySnapshot.sku, InventorySnapshot.captured_at, InventorySnapshot.on_hand)
        .where(InventorySnapshot.sku.in_(aliases_of_canonical))
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

    # Variance-based safety stock inputs (same logic as compute_demand_planning_view).
    from app.config import z_for_service_level
    sigma_daily = v.sigma_daily_raw if v else None
    sku_service_level = sku.service_level if (sku and sku.service_level is not None) else None
    effective_service_level = sku_service_level or settings.demand_service_level_default
    try:
        z_value = z_for_service_level(Decimal(str(effective_service_level)))
    except KeyError:
        z_value = z_for_service_level(settings.demand_service_level_default)

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
        sigma_daily=sigma_daily,
        z_value=z_value,
        cover_days=cover,
        overstocked_threshold_days=overstocked,
        moq=moq,
        case_pack=case_pack,
        is_reorderable=is_reorderable,
        unit_cogs=unit_cogs,
    )
    row = compute_one(inputs, as_of=now.date())

    # Math breakdown — reuse the value compute_one actually used (handles
    # both variance and flat-fallback paths so the walkthrough matches the
    # reorder_point math 1:1).
    lead_time_demand = int((inputs.daily_velocity * Decimal(inputs.lead_time_days))
                           .to_integral_value(rounding="ROUND_HALF_UP"))
    safety_buffer = row.safety_stock_units
    target_units = int((inputs.daily_velocity * Decimal(inputs.lead_time_days + inputs.cover_days))
                       .to_integral_value(rounding="ROUND_HALF_UP"))
    available = on_hand + expected_receipts

    parents, children = _bundle_relationships(db, sku, component_sku)

    return SkuDetailView(
        row=row,
        safety_stock_pct=effective_safety,
        cover_days=cover,
        sku=sku,
        weekly_velocity=_weekly_velocity(db, component_sku, as_of=now, alias_map=alias_map),
        inventory_history=_inventory_history(db, component_sku, alias_map=alias_map),
        bundle_parents=parents,
        bundle_components=children,
        lead_time_demand=lead_time_demand,
        safety_buffer=safety_buffer,
        target_units=target_units,
        available=available,
        default_lead_time_days=settings.demand_lead_time_default_days,
        default_safety_stock_pct=settings.demand_safety_stock_pct,
        demand_breakdown=_demand_breakdown(db, component_sku, as_of=now, alias_map=alias_map),
    )
