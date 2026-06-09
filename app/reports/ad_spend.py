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
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.ad_credit import AdCredit
from app.models.ad_spend import AdSpend
from app.models.gmv_max_campaign_metric import GmvMaxCampaignMetric
from app.models.order import Order
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


def compute_ad_spend_monthly(db: Session) -> AdSpendMonthly:
    """One row per calendar month that had ad spend, with gross spend
    (total_ad_spend) and ROAS, plus rolled-up totals. Months with $0 ad spend
    are omitted (ROAS is undefined there). Each month reuses the P&L engine so
    gross spend and ROAS match the rest of the app exactly."""
    from app.reports.monthly_pnl import compute_monthly_pnl

    o_lo, o_hi = db.execute(
        select(func.min(Order.placed_at), func.max(Order.placed_at))
    ).one()
    a_lo, a_hi = db.execute(
        select(func.min(AdSpend.spend_date), func.max(AdSpend.spend_date))
    ).one()
    los = [d for d in (o_lo, a_lo) if d is not None]
    his = [d for d in (o_hi, a_hi) if d is not None]
    if not los:
        return AdSpendMonthly()
    lo, hi = min(los), max(his)

    # Months that have an entered campaign metric — included even if (somehow)
    # they have no GMV-Max spend row yet, so their SKU Orders / Gross Revenue
    # still surface.
    metric_months = {
        (int(my), int(mm)) for my, mm in db.execute(
            select(GmvMaxCampaignMetric.year, GmvMaxCampaignMetric.month)
        ).all()
    }

    rows: list[AdSpendMonthKpi] = []
    sum_gross = sum_net = Decimal("0")
    y, m = lo.year, lo.month
    while (y, m) <= (hi.year, hi.month):
        pnl = compute_monthly_pnl(db, y, m)
        # GMV-Max spend ONLY (excludes settlement Shop Ads) so this page matches
        # TikTok's GMV Max Ad Cost. Shop Ads still flows through the P&L.
        if pnl.gmv_max_ad_spend > 0 or (y, m) in metric_months:
            m_end = (datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1))
            camp = compute_gmv_max_campaign_kpis(db, datetime(y, m, 1), m_end)
            rows.append(AdSpendMonthKpi(
                year=y, month=m,
                gross_spend=pnl.gmv_max_ad_spend,
                roas=pnl.gmv_max_roas,
                sku_orders=camp.sku_orders if camp.has_data else None,
                cost_per_order=camp.cost_per_order if (camp.has_data and camp.sku_orders > 0) else None,
                gross_revenue=camp.gross_revenue if camp.has_data else None,
                roi=camp.roi if (camp.has_data and camp.ad_cost > 0) else None,
            ))
            sum_gross += pnl.gmv_max_ad_spend
            sum_net += pnl.net_customer_sales
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)

    total_roas = (sum_net / sum_gross) if sum_gross else Decimal("0")
    # All-time campaign totals for the footer — reuses the same report the top
    # table uses, so the footer ties out to it exactly.
    campaign_total = compute_gmv_max_campaign_kpis(db)
    return AdSpendMonthly(
        rows=rows, total_gross=sum_gross, total_roas=total_roas,
        campaign_total=campaign_total,
    )
