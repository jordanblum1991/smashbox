"""Ad spend summary — total cost broken down by month, with sub-columns for
cash, credit, and TikTok-issued ad credits.

The source is the `AdSpend` table populated by the TikTok Ads "Cost" export.
TikTok records three buckets per (date, campaign) line:

  - Cash cost       : actual money charged to the merchant
  - Credit cost     : merchant-funded credit balance drawdown
  - Ad credit cost  : promotional credits TikTok issued to the merchant
  - Amount          : sum of the three (canonical "what TikTok counts")

Showing the three buckets separately matters because ad credits aren't a real
cash outflow — surfacing them lets us reconcile total reported spend against
out-of-pocket spend.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.ad_credit import AdCredit
from app.models.ad_spend import AdSpend
from app.models.gmv_max_daily_metric import GmvMaxDailyMetric
from app.models.order import Order
from app.reports.fiscal_calendar import fiscal_months_for, fiscal_window
from app.reports.gmv_max_campaign_kpis import GmvMaxCampaignKpis, compute_gmv_max_campaign_kpis


@dataclass
class AdSpendMonthRow:
    year: int
    month: int
    cash_cost: Decimal
    credit_cost: Decimal
    ad_credit_cost: Decimal
    total: Decimal                     # gross — what TikTok reported as spend
    manual_credit: Decimal             # manually-entered offset (may be 0)
    credit_note: str | None
    credit_id: int | None              # so the row form can target the existing record
    credit_applied_date: date | None   # specific day the credit lands on; the
                                       # P&L filters on this. None when no
                                       # credit row exists yet for this month.

    @property
    def net_total(self) -> Decimal:
        """Gross spend minus manual ad credit — the true cash cost."""
        return self.total - self.manual_credit

    @property
    def credit_saved(self) -> bool:
        """True iff an AdCredit row exists for this month — distinguishes a
        deliberately-saved $0 from a never-entered month. The form binds to
        the row's identity, not its amount, so a saved $0 is sticky."""
        return self.credit_id is not None


@dataclass
class AdSpendSummary:
    months: list[AdSpendMonthRow]
    cash_cost: Decimal
    credit_cost: Decimal
    ad_credit_cost: Decimal
    total: Decimal                     # gross totals
    manual_credit: Decimal             # all-time manual credits
    period_start: date | None
    period_end: date | None

    @property
    def net_total(self) -> Decimal:
        return self.total - self.manual_credit


def compute_ad_spend_summary(db: Session) -> AdSpendSummary:
    """All-time monthly breakdown — no period filter; the page itself is a
    cross-period summary. Joins in any manual ad credits per month, and
    surfaces months that have a credit even when there's no spend yet."""
    spend_rows = db.execute(
        select(
            func.extract("year", AdSpend.spend_date).label("y"),
            func.extract("month", AdSpend.spend_date).label("m"),
            func.coalesce(func.sum(AdSpend.cash_cost), 0).label("cash"),
            func.coalesce(func.sum(AdSpend.credit_cost), 0).label("credit"),
            func.coalesce(func.sum(AdSpend.ad_credit_cost), 0).label("ad_credit"),
            func.coalesce(func.sum(AdSpend.amount), 0).label("total"),
        )
        .group_by("y", "m")
        .order_by("y", "m")
    ).all()

    credit_rows = db.execute(
        select(
            AdCredit.applied_date, AdCredit.year, AdCredit.month,
            AdCredit.amount, AdCredit.note, AdCredit.id,
        )
    ).all()
    # Month key is derived from applied_date when present, falling back to the
    # legacy year/month columns for any row that somehow predates the backfill.
    credits: dict[tuple[int, int], tuple[Decimal, str | None, int, date | None]] = {}
    for r in credit_rows:
        if r.applied_date is not None:
            key = (r.applied_date.year, r.applied_date.month)
        else:
            key = (int(r.year), int(r.month))
        credits[key] = (Decimal(str(r.amount)), r.note, int(r.id), r.applied_date)

    months_seen: dict[tuple[int, int], AdSpendMonthRow] = {}
    for r in spend_rows:
        key = (int(r.y), int(r.m))
        credit_amt, credit_note, credit_id, credit_dt = credits.get(
            key, (Decimal("0"), None, None, None)
        )
        months_seen[key] = AdSpendMonthRow(
            year=key[0],
            month=key[1],
            cash_cost=Decimal(str(r.cash)),
            credit_cost=Decimal(str(r.credit)),
            ad_credit_cost=Decimal(str(r.ad_credit)),
            total=Decimal(str(r.total)),
            manual_credit=credit_amt,
            credit_note=credit_note,
            credit_id=credit_id,
            credit_applied_date=credit_dt,
        )
    # Months that have a credit but no spend row — surface them so the user
    # can still see/edit the credit they entered.
    for key, (amt, note, cid, credit_dt) in credits.items():
        if key not in months_seen:
            months_seen[key] = AdSpendMonthRow(
                year=key[0],
                month=key[1],
                cash_cost=Decimal("0"),
                credit_cost=Decimal("0"),
                ad_credit_cost=Decimal("0"),
                total=Decimal("0"),
                manual_credit=amt,
                credit_note=note,
                credit_id=cid,
                credit_applied_date=credit_dt,
            )

    months = [months_seen[k] for k in sorted(months_seen.keys())]

    bounds = db.execute(
        select(func.min(AdSpend.spend_date), func.max(AdSpend.spend_date))
    ).one()

    def _d(v) -> date | None:
        return v.date() if v else None

    return AdSpendSummary(
        months=months,
        cash_cost=sum((m.cash_cost for m in months), Decimal("0")),
        credit_cost=sum((m.credit_cost for m in months), Decimal("0")),
        ad_credit_cost=sum((m.ad_credit_cost for m in months), Decimal("0")),
        total=sum((m.total for m in months), Decimal("0")),
        manual_credit=sum((m.manual_credit for m in months), Decimal("0")),
        period_start=_d(bounds[0]),
        period_end=_d(bounds[1]),
    )


# ---------------------------------------------------------------------------
# Per-month KPI summary — gross spend + ROAS, the Ad Spend page's default view.
# ---------------------------------------------------------------------------

@dataclass
class AdSpendMonthKpi:
    year: int
    month: int
    gross_spend: Decimal   # GMV-Max ad spend only (before credits); excl. Shop Ads
    roas: Decimal          # Net Customer Sales / gross_spend (GMV-Max)
    # Campaign-attributed KPIs for the month (None when no metric entered).
    sku_orders: int | None = None
    cost_per_order: Decimal | None = None
    gross_revenue: Decimal | None = None
    roi: Decimal | None = None


@dataclass
class AdSpendMonthly:
    rows: list[AdSpendMonthKpi] = field(default_factory=list)
    total_gross: Decimal = Decimal("0")
    total_roas: Decimal = Decimal("0")   # Σ net sales / Σ gross spend
    # All-time campaign totals for the footer (None when none entered).
    campaign_total: GmvMaxCampaignKpis | None = None


def _window_kpi(
    db: Session, year: int, month: int, start_dt: datetime, end_dt: datetime,
) -> "tuple[AdSpendMonthKpi, Decimal] | None":
    """Campaign KPIs + ROAS for one [start_dt, end_dt) window, labeled
    (year, month). Returns (row, net_customer_sales), or None when the window
    has no GMV-Max activity. Shared by the calendar-month and fiscal views so
    the per-period math is identical."""
    from app.reports.monthly_pnl import compute_window_pnl

    camp = compute_gmv_max_campaign_kpis(db, start_dt, end_dt)
    if not camp.has_data:
        return None
    net = compute_window_pnl(db, start_dt, end_dt).net_customer_sales
    roas = (net / camp.ad_cost) if camp.ad_cost else Decimal("0")
    return (
        AdSpendMonthKpi(
            year=year, month=month,
            gross_spend=camp.ad_cost,
            roas=roas,
            sku_orders=camp.sku_orders,
            cost_per_order=camp.cost_per_order if camp.sku_orders > 0 else None,
            gross_revenue=camp.gross_revenue,
            roi=camp.roi if camp.ad_cost > 0 else None,
        ),
        net,
    )


def _aggregate(pairs: "list[tuple[AdSpendMonthKpi, Decimal]]") -> AdSpendMonthly:
    """Build the AdSpendMonthly (rows + totals) from per-period (row, net) pairs.
    Totals are aggregated FROM the rows so the footer always ties to the listing."""
    cent = Decimal("0.01")
    rows = [k for k, _ in pairs]
    sum_gross = sum((k.gross_spend for k in rows), Decimal("0"))
    sum_net = sum((n for _, n in pairs), Decimal("0"))
    sum_gr = sum((k.gross_revenue for k in rows), Decimal("0"))
    sum_sku = sum((k.sku_orders for k in rows), 0)
    total_roas = (sum_net / sum_gross) if sum_gross else Decimal("0")
    campaign_total = GmvMaxCampaignKpis(
        gross_revenue=sum_gr.quantize(cent),
        sku_orders=sum_sku,
        ad_cost=sum_gross.quantize(cent),
        cost_per_order=(sum_gross / sum_sku).quantize(cent) if sum_sku else Decimal("0"),
        roi=(sum_gr / sum_gross).quantize(cent) if sum_gross else Decimal("0"),
        has_data=bool(rows),
    )
    return AdSpendMonthly(
        rows=rows, total_gross=sum_gross, total_roas=total_roas,
        campaign_total=campaign_total,
    )


def compute_ad_spend_monthly(
    db: Session,
    start: datetime | None = None,
    end: datetime | None = None,
) -> AdSpendMonthly:
    """One row per month with GMV Max campaign activity, carrying the campaign
    KPIs (SKU Orders, Cost per Order, Gross Revenue, ROI), Total Gross Spend
    (= the campaign report's Cost), and ROAS (Net Customer Sales ÷ Cost). Totals
    are aggregated FROM the shown rows, so the footer ties to the listing.

    Sourced from the imported daily campaign metrics (`GmvMaxDailyMetric`), so it
    mirrors TikTok's GMV Max report. `start`/`end` (EXCLUSIVE end) scope to a date
    range with day accuracy — each month is clamped to the window. With no window,
    all days are covered. ROAS net comes from the P&L engine over the same
    clamped window."""
    d_lo, d_hi = db.execute(
        select(func.min(GmvMaxDailyMetric.metric_date), func.max(GmvMaxDailyMetric.metric_date))
    ).one()
    if d_lo is None:
        return AdSpendMonthly()

    w_start = start.date() if start is not None else d_lo
    w_end = end.date() if end is not None else (d_hi + timedelta(days=1))   # exclusive

    pairs: list[tuple[AdSpendMonthKpi, Decimal]] = []
    y, m = w_start.year, w_start.month
    while date(y, m, 1) < w_end:
        m_start = date(y, m, 1)
        m_end = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        clo = max(m_start, w_start)            # clamp the month to the window
        chi = min(m_end, w_end)                # [clo, chi)
        if clo < chi:
            res = _window_kpi(
                db, y, m,
                datetime.combine(clo, datetime.min.time()),
                datetime.combine(chi, datetime.min.time()),
            )
            if res is not None:
                pairs.append(res)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)

    return _aggregate(pairs)


def compute_ad_spend_fiscal(
    db: Session, year: int, month: int, mode: str,
) -> AdSpendMonthly:
    """GMV Max campaign KPIs over Smashbox FISCAL periods (29th → 28th), with the
    same columns as the monthly view. `mode` selects the span:
      'month' → one fiscal-month row    'ytd' → fiscal Jan..month    'year' → 12
    Each fiscal month is computed over its own [start, end) window via the shared
    `_window_kpi`, so the numbers match the P&L fiscal view to the cent. Fiscal
    months with no GMV-Max activity are omitted (mirrors the calendar view)."""
    pairs: list[tuple[AdSpendMonthKpi, Decimal]] = []
    for mm in fiscal_months_for(mode, month):
        start_d, end_incl = fiscal_window(year, mm)
        res = _window_kpi(
            db, year, mm,
            datetime(start_d.year, start_d.month, start_d.day),
            datetime(end_incl.year, end_incl.month, end_incl.day) + timedelta(days=1),  # exclusive
        )
        if res is not None:
            pairs.append(res)
    return _aggregate(pairs)


# ---------------------------------------------------------------------------
# Per-DAY KPI listing — campaign-attributed figures straight from the daily
# GMV-Max metric, for a specified date range. Attributed-only by design (no
# blended ROAS): cost, SKU orders, cost/order, gross revenue, and ROI all come
# from GmvMaxDailyMetric and aggregate exactly — no per-day whole-shop P&L.
# ---------------------------------------------------------------------------

@dataclass
class AdSpendDayRow:
    day: date
    gross_spend: Decimal           # GMV-Max Cost for the day
    sku_orders: int
    gross_revenue: Decimal

    @property
    def cost_per_order(self) -> Decimal | None:
        if self.sku_orders <= 0:
            return None
        return (self.gross_spend / self.sku_orders).quantize(Decimal("0.01"))

    @property
    def roi(self) -> Decimal | None:
        if self.gross_spend <= 0:
            return None
        return (self.gross_revenue / self.gross_spend).quantize(Decimal("0.01"))


@dataclass
class AdSpendDailyView:
    rows: list[AdSpendDayRow]
    start: date
    end: date                      # inclusive end (the day the user picked)
    total: GmvMaxCampaignKpis      # window aggregate (carries has_data)


def compute_ad_spend_daily(db: Session, start: date, end: date) -> AdSpendDailyView:
    """One row per day in [start, end] (inclusive) that had GMV-Max activity,
    with campaign-attributed figures from the daily metric. Days with no
    campaign data are omitted (mirrors the monthly view's "months with
    activity"). Totals aggregate the window via the shared KPI helper, so the
    footer ties to TikTok's GMV Max report for the range."""
    rows_raw = db.execute(
        select(
            GmvMaxDailyMetric.metric_date,
            GmvMaxDailyMetric.cost,
            GmvMaxDailyMetric.sku_orders,
            GmvMaxDailyMetric.gross_revenue,
        )
        .where(GmvMaxDailyMetric.metric_date >= start)
        .where(GmvMaxDailyMetric.metric_date <= end)
        .order_by(GmvMaxDailyMetric.metric_date)
    ).all()
    rows = [
        AdSpendDayRow(
            day=d,
            gross_spend=Decimal(str(cost or 0)),
            sku_orders=int(sku or 0),
            gross_revenue=Decimal(str(gr or 0)),
        )
        for d, cost, sku, gr in rows_raw
        if (cost or 0) or (sku or 0) or (gr or 0)
    ]
    total = compute_gmv_max_campaign_kpis(
        db,
        datetime.combine(start, datetime.min.time()),
        datetime.combine(end + timedelta(days=1), datetime.min.time()),  # exclusive end
    )
    return AdSpendDailyView(rows=rows, start=start, end=end, total=total)
