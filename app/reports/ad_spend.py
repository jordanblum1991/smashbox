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
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.ad_spend import AdSpend


@dataclass
class AdSpendMonthRow:
    year: int
    month: int
    cash_cost: Decimal
    credit_cost: Decimal
    ad_credit_cost: Decimal
    total: Decimal


@dataclass
class AdSpendSummary:
    months: list[AdSpendMonthRow]
    cash_cost: Decimal
    credit_cost: Decimal
    ad_credit_cost: Decimal
    total: Decimal
    period_start: date | None
    period_end: date | None


def compute_ad_spend_summary(db: Session) -> AdSpendSummary:
    """All-time monthly breakdown — no period filter; the page itself is a
    cross-period summary."""
    rows = db.execute(
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

    months = [
        AdSpendMonthRow(
            year=int(r.y),
            month=int(r.m),
            cash_cost=Decimal(str(r.cash)),
            credit_cost=Decimal(str(r.credit)),
            ad_credit_cost=Decimal(str(r.ad_credit)),
            total=Decimal(str(r.total)),
        )
        for r in rows
    ]

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
        period_start=_d(bounds[0]),
        period_end=_d(bounds[1]),
    )
