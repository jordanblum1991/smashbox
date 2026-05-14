"""Reconciliation — does the P&L tie to TikTok's settlement file?

For a given month, this report compares the totals we computed from the orders
file ("System Calculated") against what TikTok reports in the settlement file
("TikTok Settlement Total"), joined ON THE SAME orders. Variance is then
decomposed into:

  - Timing:        orders placed in the period but not yet settled by TikTok.
  - Mapping/error: variance on orders that ARE settled — should be zero.

The seller-funded discount split check is kept exactly as-is — it asserts the
load-bearing invariant Outlandish + Smashbox == TikTok total seller-funded.
"""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import case, distinct, func, select
from sqlalchemy.orm import Session

from app.models.order import Order, OrderType
from app.models.payout import Payout
from app.models.settlement import Settlement


# ---- Status: what files are loaded, what dates are covered -----------------

@dataclass
class FileStatus:
    orders_loaded: bool
    settlements_loaded: bool
    orders_count: int
    settlements_count: int
    latest_order_date: datetime | None
    latest_settlement_paid_date: datetime | None
    latest_settlement_settled_date: datetime | None


def _file_status(db: Session) -> FileStatus:
    orders_count = db.execute(select(func.count(Order.id))).scalar() or 0
    settlements_count = db.execute(select(func.count(Settlement.id))).scalar() or 0
    return FileStatus(
        orders_loaded=orders_count > 0,
        settlements_loaded=settlements_count > 0,
        orders_count=orders_count,
        settlements_count=settlements_count,
        latest_order_date=db.execute(select(func.max(Order.placed_at))).scalar(),
        latest_settlement_paid_date=db.execute(select(func.max(Settlement.paid_date))).scalar(),
        latest_settlement_settled_date=db.execute(select(func.max(Settlement.settled_date))).scalar(),
    )


# ---- One reconciliation row, with diagnostics ------------------------------

@dataclass
class ReconciliationLine:
    label: str
    system_calculated: Decimal       # what we computed from orders/workbook
    tiktok_settlement: Decimal       # what TikTok reports in settlement
    likely_cause: str | None = None  # human-readable diagnosis, if non-OK
    tolerance_cents: int = 1
    is_count: bool = False           # render as integer (e.g. policy violation count)

    @property
    def variance(self) -> Decimal:
        return self.system_calculated - self.tiktok_settlement

    @property
    def ok(self) -> bool:
        return abs(self.variance) <= Decimal(self.tolerance_cents) / Decimal(100)


# ---- Drill-down rows -------------------------------------------------------

@dataclass
class OrderRow:
    tiktok_order_id: str
    placed_at: datetime
    gross_sales: Decimal
    settled: bool
    settlement_gross: Decimal


@dataclass
class OrphanSettlementRow:
    tiktok_order_id: str
    paid_date: datetime | None
    settlement_gross: Decimal


# ---- Full report shape -----------------------------------------------------

@dataclass
class ReconciliationReport:
    year: int
    month: int
    status: FileStatus
    lines: list[ReconciliationLine]

    # System side
    period_orders_total: Decimal
    period_orders_count: int
    period_settled_orders_total: Decimal
    period_settled_orders_count: int
    period_unsettled_orders_total: Decimal
    period_unsettled_orders_count: int

    # Drill-down lists (truncated for display)
    paid_orders: list[OrderRow] = field(default_factory=list)
    timing_orders: list[OrderRow] = field(default_factory=list)        # in period, not yet settled
    true_variance_orders: list[OrderRow] = field(default_factory=list) # settled, but our gross != TikTok gross
    orphan_settlements: list[OrphanSettlementRow] = field(default_factory=list)

    @property
    def timing_amount(self) -> Decimal:
        return self.period_unsettled_orders_total

    @property
    def true_error_amount(self) -> Decimal:
        """Variance left after removing the timing portion. Should be ~zero."""
        return sum(
            (o.gross_sales - o.settlement_gross for o in self.true_variance_orders),
            Decimal("0"),
        )


# ---- The report ------------------------------------------------------------

def reconcile_month(db: Session, year: int, month: int) -> ReconciliationReport:
    start = datetime(year, month, 1)
    end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)

    status = _file_status(db)

    # All paid orders placed in the period, with their settlement totals
    # joined ON tiktok_order_id (NOT on settlement.paid_date — that was the
    # bug that made May look like a $6,872 variance).
    settle_subq = (
        select(
            Settlement.tiktok_order_id.label("oid"),
            func.coalesce(func.sum(Settlement.gross_sales), Decimal("0")).label("settle_gross"),
        )
        .group_by(Settlement.tiktok_order_id)
        .subquery()
    )

    rows = db.execute(
        select(
            Order.tiktok_order_id,
            Order.placed_at,
            Order.gross_sales,
            settle_subq.c.settle_gross,
        )
        .outerjoin(settle_subq, settle_subq.c.oid == Order.tiktok_order_id)
        .where(Order.placed_at >= start, Order.placed_at < end)
        .where(Order.order_type == OrderType.PAID)
        .order_by(Order.placed_at)
    ).all()

    paid_orders: list[OrderRow] = []
    settled_total = Decimal("0")
    settled_count = 0
    unsettled_total = Decimal("0")
    unsettled_count = 0
    period_total = Decimal("0")
    true_variance_orders: list[OrderRow] = []
    timing_orders: list[OrderRow] = []

    for tiktok_oid, placed, gross, settle_gross in rows:
        g = Decimal(str(gross or 0))
        s = Decimal(str(settle_gross)) if settle_gross is not None else Decimal("0")
        is_settled = settle_gross is not None
        period_total += g
        row = OrderRow(
            tiktok_order_id=tiktok_oid,
            placed_at=placed,
            gross_sales=g,
            settled=is_settled,
            settlement_gross=s,
        )
        paid_orders.append(row)
        if is_settled:
            settled_total += g
            settled_count += 1
            if abs(g - s) > Decimal("0.01"):
                true_variance_orders.append(row)
        else:
            unsettled_total += g
            unsettled_count += 1
            timing_orders.append(row)

    # Aggregated comparison values for the headline line
    tiktok_total_for_period_orders = sum(
        (o.settlement_gross for o in paid_orders), Decimal("0")
    )

    # ---- Seller-funded check (unchanged — load-bearing invariant) -----------
    derived_sf = db.execute(
        select(
            func.coalesce(func.sum(Order.seller_funded_outlandish), 0)
            + func.coalesce(func.sum(Order.seller_funded_smashbox), 0)
        )
        .where(Order.placed_at >= start, Order.placed_at < end)
        .where(Order.order_type == OrderType.PAID)
    ).scalar() or 0

    tiktok_sf = db.execute(
        select(func.coalesce(func.sum(Order.seller_funded_discount_total), 0))
        .where(Order.placed_at >= start, Order.placed_at < end)
        .where(Order.order_type == OrderType.PAID)
    ).scalar() or 0

    # ---- Policy violations count -------------------------------------------
    policy_violations = db.execute(
        select(func.coalesce(func.count(Order.id), 0))
        .where(Order.placed_at >= start, Order.placed_at < end)
        .where(Order.discount_policy_violation.is_(True))
    ).scalar() or 0

    # ---- Payouts (informational) -------------------------------------------
    tiktok_payout_net = db.execute(
        select(func.coalesce(func.sum(Payout.net_amount), 0))
        .where(Payout.paid_at >= start, Payout.paid_at < end)
    ).scalar() or 0

    # ---- Orphan settlements (settlements with no matching order) -----------
    # Scoped to the same period via Settlement.paid_date so the user sees only
    # orphans relevant to this report.
    orphan_rows = db.execute(
        select(
            Settlement.tiktok_order_id,
            func.min(Settlement.paid_date),
            func.coalesce(func.sum(Settlement.gross_sales), 0),
        )
        .outerjoin(Order, Order.tiktok_order_id == Settlement.tiktok_order_id)
        .where(Order.id.is_(None))
        .where(Settlement.paid_date >= start, Settlement.paid_date < end)
        .group_by(Settlement.tiktok_order_id)
    ).all()
    orphan_settlements = [
        OrphanSettlementRow(
            tiktok_order_id=r[0],
            paid_date=r[1],
            settlement_gross=Decimal(str(r[2] or 0)),
        )
        for r in orphan_rows
    ]

    # ---- Build the lines + likely-cause diagnoses --------------------------
    lines: list[ReconciliationLine] = []

    gross_line = ReconciliationLine(
        label="Gross sales (paid orders in this period vs settlements for those orders)",
        system_calculated=period_total,
        tiktok_settlement=tiktok_total_for_period_orders,
    )
    gross_line.likely_cause = _diagnose_gross(
        status=status,
        period_total=period_total,
        period_count=len(paid_orders),
        settled_total=settled_total,
        settled_count=settled_count,
        unsettled_total=unsettled_total,
        unsettled_count=unsettled_count,
        true_variance_count=len(true_variance_orders),
        orphan_count=len(orphan_settlements),
    )
    lines.append(gross_line)

    sf_line = ReconciliationLine(
        label="Seller-funded split (Outlandish + Smashbox) vs TikTok total",
        system_calculated=Decimal(str(derived_sf)),
        tiktok_settlement=Decimal(str(tiktok_sf)),
        tolerance_cents=0,
    )
    sf_line.likely_cause = None if sf_line.ok else (
        "Split invariant broken — this MUST be exact. "
        "Inspect app/rules/seller_funded_split.py."
    )
    lines.append(sf_line)

    policy_line = ReconciliationLine(
        label="Policy violations (orders with seller-funded > 30% of MSRP)",
        system_calculated=Decimal(str(policy_violations)),
        tiktok_settlement=Decimal("0"),
        tolerance_cents=0,
        is_count=True,
    )
    policy_line.likely_cause = (
        f"{int(policy_violations)} order(s) exceed the 30% policy cap. "
        "Inspect those orders to see whether the discount was intentional."
    ) if policy_violations else None
    lines.append(policy_line)

    payout_line = ReconciliationLine(
        label="Payouts (informational — net cash TikTok paid this month)",
        system_calculated=Decimal("0"),
        tiktok_settlement=Decimal(str(tiktok_payout_net)),
    )
    lines.append(payout_line)

    return ReconciliationReport(
        year=year,
        month=month,
        status=status,
        lines=lines,
        period_orders_total=period_total,
        period_orders_count=len(paid_orders),
        period_settled_orders_total=settled_total,
        period_settled_orders_count=settled_count,
        period_unsettled_orders_total=unsettled_total,
        period_unsettled_orders_count=unsettled_count,
        paid_orders=paid_orders,
        timing_orders=timing_orders,
        true_variance_orders=true_variance_orders,
        orphan_settlements=orphan_settlements,
    )


def _diagnose_gross(
    *,
    status: FileStatus,
    period_total: Decimal,
    period_count: int,
    settled_total: Decimal,
    settled_count: int,
    unsettled_total: Decimal,
    unsettled_count: int,
    true_variance_count: int,
    orphan_count: int,
) -> str | None:
    """Classify the variance into one of the categories the user asked for."""
    if not status.orders_loaded:
        return "Missing orders file — upload an orders export to populate this."
    if not status.settlements_loaded:
        return "Missing settlement file — upload a settlement export."
    if period_count == 0:
        return "No paid orders in this period."
    if settled_count == 0:
        if status.latest_settlement_paid_date and status.latest_settlement_paid_date < datetime.now():
            return (
                "Timing: settlement file doesn't cover this period yet. "
                f"All {period_count} orders are pending; upload an updated "
                "settlement export when TikTok publishes one."
            )
        return (
            "Settlement file is loaded but no rows match any order in this "
            "period — possible mapping problem. Check /reports/settlement-only-orders."
        )
    if unsettled_count and not true_variance_count and not orphan_count:
        return (
            f"Timing: {unsettled_count} of {period_count} orders are pending "
            f"settlement (${unsettled_total}). The settled portion ties exactly."
        )
    if true_variance_count and unsettled_count:
        return (
            f"Mixed: {unsettled_count} pending (${unsettled_total}, timing) "
            f"+ {true_variance_count} settled orders with a true mismatch — investigate."
        )
    if true_variance_count:
        return (
            f"True reconciliation error on {true_variance_count} settled "
            "order(s). Drill in below."
        )
    if orphan_count:
        return (
            f"{orphan_count} settlement row(s) have no matching order — see "
            "the Orphan Orders report. Most likely the orders file's date range "
            "doesn't cover the settlement window."
        )
    return None  # all clean
