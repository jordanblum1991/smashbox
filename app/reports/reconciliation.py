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
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import case, distinct, func, select
from sqlalchemy.orm import Session

from app.models.order import Order, OrderType
from app.models.payout import Payout
from app.models.settlement import Settlement
from app.models.tiktok_daily_metric import TikTokDailyMetric
from app.reports.monthly_pnl import compute_monthly_pnl
from app.services.reporting_tz import (
    placed_local_date,
    placed_window,
    shop_boundary_to_source,
    today_local,
)


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


@dataclass
class PayoutRow:
    """One row of the per-payout cash reconciliation drill-down.

    `expected` is the sum of Settlement.net_order_margin across all settlements
    whose `linked_payout_id` matches this payout. `actual` is what TikTok
    actually transferred to the bank (`Payout.net_amount`). They should tie;
    the variance flags timing differences, missing settlement files, or
    adjustment-driven gaps.
    """
    payout_id: str
    paid_at: datetime
    expected: Decimal
    actual: Decimal

    @property
    def variance(self) -> Decimal:
        return self.expected - self.actual

    @property
    def ok(self) -> bool:
        return abs(self.variance) <= Decimal("0.01")


# ---- Full report shape -----------------------------------------------------

@dataclass
class SalesReconciliation:
    """Three-row reconciliation that closes the gap between TikTok Seller
    Center's "Sales" tile (pre-refund) and our P&L's Net Customer Sales
    (post-refund, accounting-standard).

    Maps directly to user-facing language: "TikTok shows X, we show Y, the
    gap is refunds."
    """
    tiktok_equivalent_sales: Decimal  # what Seller Center's "Sales" tile shows
    refunds: Decimal                  # subtracted from sales for accounting
    net_customer_sales: Decimal       # what our P&L shows

    @property
    def gap(self) -> Decimal:
        return self.tiktok_equivalent_sales - self.net_customer_sales


@dataclass
class MonthlySalesReconciliation:
    """One row of the by-month sales-reconciliation table on the Reconciliation
    page. Same three columns as SalesReconciliation, tagged with year/month so
    the template can highlight the currently-selected month."""
    year: int
    month: int
    tiktok_equivalent_sales: Decimal
    refunds: Decimal
    net_customer_sales: Decimal
    orders_count: int

    @property
    def gap(self) -> Decimal:
        return self.tiktok_equivalent_sales - self.net_customer_sales


@dataclass
class DailySalesReconciliation:
    """One row of the by-day sales-reconciliation table — used to drill into
    a specific month and isolate which day TikTok and our P&L diverge.

    `gmv` is OUR GMV-equivalent for the day, computed the same way TikTok
    defines GMV (gross + shipping − seller promos − platform co-funding), so
    it compares like-for-like against `tiktok_gmv`. Refunds are NOT subtracted
    from either (GMV is pre-refund); `net_customer_sales` is the refund-adjusted
    P&L figure, shown alongside for context.

    `tiktok_gmv` / `tiktok_orders` come from the Shop Analytics import (the
    same numbers TikTok shows on its dashboard). They're None when no analytics
    file has been uploaded yet — the template falls back to "—" in that case so
    the row still renders.
    """
    day: date
    gmv: Decimal                      # our GMV-equivalent for this day
    refunds: Decimal
    net_customer_sales: Decimal
    orders_count: int
    tiktok_gmv: Decimal | None = None  # TikTok-reported GMV for this day
    tiktok_orders: int | None = None   # TikTok-reported order count

    @property
    def tiktok_variance(self) -> Decimal | None:
        """Our GMV minus TikTok's reported GMV for the same day. Should be
        exactly zero when everything reconciles; non-zero = the day worth
        investigating. None when no TikTok data is available."""
        if self.tiktok_gmv is None:
            return None
        return self.gmv - self.tiktok_gmv


def daily_sales_reconciliation(
    db: Session, year: int, month: int
) -> list[DailySalesReconciliation]:
    """Sales reconciliation broken into days for a given month — one row per
    calendar day that had at least one PAID order placed on it. Same three
    headline figures as the monthly view (TikTok Sales, Refunds, Net Customer
    Sales) plus an order count for context.

    Bucketed by the shop-local (Seller Center) day: placed_at is converted to
    America/Los_Angeles per-row, because a single month can straddle the DST
    boundary so a constant SQL offset would mis-bucket early-month days. The
    window is the shop-local month converted into placed_at's source zone.
    """
    start = datetime(year, month, 1)
    end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)

    rows = db.execute(
        select(
            Order.placed_at,
            Order.gross_sales,
            Order.platform_discount_total,
            Order.seller_funded_outlandish,
            Order.seller_funded_smashbox,
            Order.payment_platform_discount,
            Order.shipping_revenue,
            Order.refunds,
        )
        .where(Order.placed_at >= shop_boundary_to_source(start),
               Order.placed_at < shop_boundary_to_source(end))
        .where(Order.order_type == OrderType.PAID)
    ).all()

    # Pull TikTok's reported daily GMV + order count for the same window so the
    # template can show a true side-by-side comparison.
    tt_rows = db.execute(
        select(
            TikTokDailyMetric.metric_date,
            TikTokDailyMetric.gmv,
            TikTokDailyMetric.orders,
        )
        .where(TikTokDailyMetric.metric_date >= start.date())
        .where(TikTokDailyMetric.metric_date < end.date())
    ).all()
    tt_by_day: dict[date, tuple[Decimal, int]] = {
        r[0]: (Decimal(str(r[1])), int(r[2] or 0)) for r in tt_rows
    }

    # Union the day keys: orders-only days, analytics-only days, and overlap.
    # Aggregate per shop-local day (placed_local_date handles the DST-varying
    # offset per order).
    acc: dict[date, dict] = {}
    for placed, gross, plat, outl, smash, ppd, ship_rev, refund in rows:
        day = placed_local_date(placed)
        a = acc.setdefault(day, {"gmv": Decimal("0"), "pre": Decimal("0"),
                                 "refund": Decimal("0"), "count": 0})
        g = Decimal(str(gross or 0))
        p = Decimal(str(plat or 0))
        o = Decimal(str(outl or 0))
        s = Decimal(str(smash or 0))
        # GMV (TikTok definition): + shipping, − seller promos, − all platform
        # co-funding (SKU platform discount + payment platform discount).
        a["gmv"] += (g + Decimal(str(ship_rev or 0)) - o - s - p
                     - Decimal(str(ppd or 0)))
        # Pre-refund product Sales — feeds net_customer_sales (− refunds).
        a["pre"] += g - p - o - s
        a["refund"] += Decimal(str(refund or 0))
        a["count"] += 1
    our_by_day: dict[date, dict] = {
        day: {
            "gmv": a["gmv"],
            "refund": a["refund"],
            "net": a["pre"] - a["refund"],
            "count": a["count"],
        }
        for day, a in acc.items()
    }

    all_days = sorted(set(our_by_day) | set(tt_by_day))
    out: list[DailySalesReconciliation] = []
    for day in all_days:
        ours = our_by_day.get(day, {
            "gmv": Decimal("0"), "refund": Decimal("0"),
            "net": Decimal("0"), "count": 0,
        })
        tt = tt_by_day.get(day)
        out.append(DailySalesReconciliation(
            day=day,
            gmv=ours["gmv"],
            refunds=ours["refund"],
            net_customer_sales=ours["net"],
            orders_count=ours["count"],
            tiktok_gmv=tt[0] if tt else None,
            tiktok_orders=tt[1] if tt else None,
        ))
    return out


# ---- reconciliation-break alerting -----------------------------------------
#
# A *settled* day where our GMV diverges from TikTok's reported GMV is a real
# signal (a missed import, a TikTok-side change) — the offset sweep showed the
# bucketing model ties settled months to $0.00/day, so a non-zero settled-day
# variance isn't timezone noise. We exclude the most recent few days, where both
# sides are still provisional (the known current-month drift).

@dataclass
class ReconBreakDay:
    day: date
    gmv: Decimal          # our GMV-equivalent for the day
    tiktok_gmv: Decimal   # TikTok-reported GMV
    variance: Decimal     # gmv − tiktok_gmv


def recent_recon_breaks(
    db: Session, *, today: date | None = None, lookback_days: int = 30,
    settle_grace_days: int = 3, threshold: Decimal = Decimal("1.00"),
) -> list[ReconBreakDay]:
    """Settled days in the trailing window whose GMV diverges from TikTok's by
    more than `threshold`. The most recent `settle_grace_days` days are excluded
    (still provisional on both sides → expected drift, not a break)."""
    today = today or today_local()
    window_end = today - timedelta(days=settle_grace_days)
    window_start = today - timedelta(days=lookback_days)
    if window_end < window_start:
        return []

    months: list[tuple[int, int]] = []
    y, m = window_start.year, window_start.month
    while (y, m) <= (window_end.year, window_end.month):
        months.append((y, m))
        m, y = (1, y + 1) if m == 12 else (m + 1, y)

    breaks: list[ReconBreakDay] = []
    for (yy, mm) in months:
        for r in daily_sales_reconciliation(db, yy, mm):
            if not (window_start <= r.day <= window_end):
                continue
            v = r.tiktok_variance
            if r.tiktok_gmv is None or v is None or abs(v) <= threshold:
                continue
            breaks.append(ReconBreakDay(day=r.day, gmv=r.gmv, tiktok_gmv=r.tiktok_gmv, variance=v))
    breaks.sort(key=lambda b: b.day)
    return breaks


_recon_cache: dict = {"at": float("-inf"), "data": None}
_RECON_TTL_SECONDS = 600  # 10 min — the per-request callers read this cache


def get_recon_break_summary(db: Session, *, ttl: float = _RECON_TTL_SECONDS) -> dict:
    """TTL-cached `{count, worst_day, worst_variance}` so the dashboard/nav can
    check for reconciliation breaks every request without recomputing the
    multi-month daily recon each time. Mirrors get_inventory_alert_summary."""
    now = time.monotonic()
    if _recon_cache["data"] is None or (now - _recon_cache["at"]) > ttl:
        breaks = recent_recon_breaks(db)
        worst = max(breaks, key=lambda b: abs(b.variance)) if breaks else None
        _recon_cache["data"] = {
            "count": len(breaks),
            "worst_day": worst.day if worst else None,
            "worst_variance": worst.variance if worst else Decimal("0"),
        }
        _recon_cache["at"] = now
    return _recon_cache["data"]


def _reset_recon_cache() -> None:
    """Test hook — clear the cached summary so a fresh DB recomputes."""
    _recon_cache["data"] = None
    _recon_cache["at"] = float("-inf")


def yearly_sales_reconciliation(
    db: Session, year: int
) -> list[MonthlySalesReconciliation]:
    """Sales reconciliation for every month of `year` that has activity.
    Months with zero orders AND zero refunds are skipped so the table stays
    tight when only part of the year has data."""
    out: list[MonthlySalesReconciliation] = []
    for m in range(1, 13):
        pnl = compute_monthly_pnl(db, year, m)
        if pnl.orders_count == 0 and pnl.refunds == 0:
            continue
        out.append(MonthlySalesReconciliation(
            year=year,
            month=m,
            tiktok_equivalent_sales=pnl.sales_pre_refund,
            refunds=pnl.refunds,
            net_customer_sales=pnl.net_customer_sales,
            orders_count=pnl.orders_count,
        ))
    return out


@dataclass
class GmvTieOutRow:
    """One month of the GMV tie-out: our computed GMV (from orders) vs TikTok's
    stated GMV (the daily Shop Analytics export). `stated_gmv` is None for months
    with no analytics loaded — `variance` is then None (nothing to tie to)."""
    year: int
    month: int
    computed_gmv: Decimal
    stated_gmv: Decimal | None = None

    @property
    def variance(self) -> Decimal | None:
        if self.stated_gmv is None:
            return None
        return self.computed_gmv - self.stated_gmv


def gmv_tie_out(db: Session, year: int) -> list[GmvTieOutRow]:
    """Per-month GMV tie-out for `year`: our computed GMV vs TikTok's stated
    GMV (summed from TikTokDailyMetric). Months with neither are omitted. The
    two should agree to the cent wherever the analytics export covers the
    month; a non-zero variance flags a month worth investigating."""
    stated: dict[int, Decimal] = {}
    for d, g in db.execute(
        select(TikTokDailyMetric.metric_date, TikTokDailyMetric.gmv)
        .where(TikTokDailyMetric.metric_date >= date(year, 1, 1))
        .where(TikTokDailyMetric.metric_date < date(year + 1, 1, 1))
    ).all():
        stated[d.month] = stated.get(d.month, Decimal("0")) + Decimal(str(g or 0))

    rows: list[GmvTieOutRow] = []
    for m in range(1, 13):
        computed = compute_monthly_pnl(db, year, m).gmv
        st = stated.get(m)
        if computed == 0 and st is None:
            continue
        rows.append(GmvTieOutRow(year=year, month=m, computed_gmv=computed, stated_gmv=st))
    return rows


@dataclass
class ReconciliationReport:
    year: int
    month: int
    status: FileStatus
    lines: list[ReconciliationLine]
    sales: SalesReconciliation        # top-of-page sales reconciliation

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
    payouts: list[PayoutRow] = field(default_factory=list)             # per-payout cash reconciliation

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
    p_start, p_end = placed_window(start, end)

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
        .where(Order.placed_at >= p_start, Order.placed_at < p_end)
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
        .where(Order.placed_at >= p_start, Order.placed_at < p_end)
        .where(Order.order_type == OrderType.PAID)
    ).scalar() or 0

    tiktok_sf = db.execute(
        select(func.coalesce(func.sum(Order.seller_funded_discount_total), 0))
        .where(Order.placed_at >= p_start, Order.placed_at < p_end)
        .where(Order.order_type == OrderType.PAID)
    ).scalar() or 0

    # ---- Policy violations count -------------------------------------------
    policy_violations = db.execute(
        select(func.coalesce(func.count(Order.id), 0))
        .where(Order.placed_at >= p_start, Order.placed_at < p_end)
        .where(Order.discount_policy_violation.is_(True))
    ).scalar() or 0

    # ---- Payouts (real cash reconciliation) ---------------------------------
    # For payouts that landed in this period, compare:
    #   System Calculated = sum of Settlement.net_order_margin across settlements
    #                       whose linked_payout_id matches a payout in the period
    #   TikTok Settlement = sum of Payout.net_amount in the period (actual cash)
    period_payouts = db.execute(
        select(Payout)
        .where(Payout.paid_at >= start, Payout.paid_at < end)
        .order_by(Payout.paid_at)
    ).scalars().all()

    period_payout_ids = {p.payout_id for p in period_payouts}
    if period_payout_ids:
        expected_by_payout = dict(
            db.execute(
                select(
                    Settlement.linked_payout_id,
                    func.coalesce(func.sum(Settlement.net_order_margin), 0),
                )
                .where(Settlement.linked_payout_id.in_(period_payout_ids))
                .group_by(Settlement.linked_payout_id)
            ).all()
        )
    else:
        expected_by_payout = {}

    payout_rows = [
        PayoutRow(
            payout_id=p.payout_id,
            paid_at=p.paid_at,
            expected=Decimal(str(expected_by_payout.get(p.payout_id, 0))),
            actual=Decimal(str(p.net_amount)),
        )
        for p in period_payouts
    ]
    payouts_expected_total = sum((r.expected for r in payout_rows), Decimal("0"))
    payouts_actual_total = sum((r.actual for r in payout_rows), Decimal("0"))

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
        "Drill in at /reports/policy-violations."
    ) if policy_violations else None
    lines.append(policy_line)

    payout_line = ReconciliationLine(
        label="Payouts — expected (sum of settlement net) vs delivered (bank)",
        system_calculated=payouts_expected_total,
        tiktok_settlement=payouts_actual_total,
    )
    payout_line.likely_cause = _diagnose_payouts(
        payout_count=len(payout_rows),
        variance=payouts_expected_total - payouts_actual_total,
    )
    lines.append(payout_line)

    # ---- Sales reconciliation block (top of page) --------------------------
    pnl_for_period = compute_monthly_pnl(db, year, month)
    sales = SalesReconciliation(
        tiktok_equivalent_sales=pnl_for_period.sales_pre_refund,
        refunds=pnl_for_period.refunds,
        net_customer_sales=pnl_for_period.net_customer_sales,
    )

    return ReconciliationReport(
        year=year,
        month=month,
        status=status,
        lines=lines,
        sales=sales,
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
        payouts=payout_rows,
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


def _diagnose_payouts(*, payout_count: int, variance: Decimal) -> str | None:
    if payout_count == 0:
        return (
            "No payouts loaded for this period — upload a payouts-income file "
            "to enable cash reconciliation."
        )
    if abs(variance) <= Decimal("0.01"):
        return None
    # Positive variance → settlements say TikTok owes more than they paid.
    # Negative variance → TikTok paid more than our settlements account for.
    sign = "more than" if variance > 0 else "less than"
    return (
        f"Settlements say TikTok should have paid {sign} the actual transfers. "
        "Likely causes: missing settlement file for some payouts, statement-level "
        "adjustments not reflected in net_order_margin, or a reserve being held."
    )
