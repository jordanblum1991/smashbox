"""Ad budget tracking — running spend vs. an allocated budget.

Actual spend auto-pulls from the daily GMV-Max ad cost (`GmvMaxDailyMetric`)
over the budget's date range; manual dated promotions (`AdBudgetPromotion`)
also reduce the available balance from their date. Pure computation: reads the
ORM, returns dataclasses, writes nothing.

Available, on any day = budget.amount − cumulative(GMV-Max spend + promotions)
through that day. The daily ledger runs from start_date to min(end_date, today)
so it shows actuals through today (no empty future rows). A budget whose
start_date is still in the future renders as "not started" — full budget
available, empty ledger.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.ad_budget import AdBudget
from app.models.gmv_max_daily_metric import GmvMaxDailyMetric
from app.services.reporting_tz import today_local

_CENT = Decimal("0.01")


@dataclass
class AdBudgetDayRow:
    day: date
    ad_spend: Decimal             # GMV-Max cost that day ($0 if none)
    promotions: Decimal           # promotion carve-outs dated that day
    committed_to_date: Decimal    # running Σ(ad_spend + promotions) from start
    available: Decimal            # budget.amount − committed_to_date


@dataclass
class AdBudgetPromoRow:
    id: int
    name: str
    amount: Decimal
    promo_date: date
    note: str | None


@dataclass
class AdBudgetView:
    budget: AdBudget
    rows: list[AdBudgetDayRow] = field(default_factory=list)
    promotions: list[AdBudgetPromoRow] = field(default_factory=list)

    budget_amount: Decimal = Decimal("0")
    total_ad_spend: Decimal = Decimal("0")
    total_promotions: Decimal = Decimal("0")
    total_committed: Decimal = Decimal("0")
    available: Decimal = Decimal("0")
    pct_used: Decimal = Decimal("0")           # 0..100+

    days_total: int = 0
    days_elapsed: int = 0
    days_remaining: int = 0

    avg_daily_spend: Decimal = Decimal("0")
    projected_total: Decimal = Decimal("0")    # burn-rate landing estimate

    is_over_budget: bool = False
    not_started: bool = False                  # today < start_date
    as_of: date | None = None                  # the through-day of the ledger


def _daily_spend(db: Session, start: date, end: date) -> dict[date, Decimal]:
    """GMV-Max cost per day in [start, end] inclusive (only days with rows)."""
    rows = db.execute(
        select(GmvMaxDailyMetric.metric_date, GmvMaxDailyMetric.cost)
        .where(GmvMaxDailyMetric.metric_date >= start)
        .where(GmvMaxDailyMetric.metric_date <= end)
    ).all()
    return {d: Decimal(str(c or 0)) for d, c in rows}


def compute_budget_view(db: Session, budget: AdBudget, *, today: date | None = None) -> AdBudgetView:
    today = today or today_local()
    amount = Decimal(str(budget.amount))
    days_total = (budget.end_date - budget.start_date).days + 1

    promo_rows = [
        AdBudgetPromoRow(id=p.id, name=p.name, amount=Decimal(str(p.amount)),
                         promo_date=p.promo_date, note=p.note)
        for p in budget.promotions
    ]
    promos_by_day: dict[date, Decimal] = {}
    for p in promo_rows:
        promos_by_day[p.promo_date] = promos_by_day.get(p.promo_date, Decimal("0")) + p.amount
    total_promotions = sum((p.amount for p in promo_rows), Decimal("0"))

    not_started = today < budget.start_date
    if not_started:
        # Budget hasn't begun — empty ledger, full budget available.
        return AdBudgetView(
            budget=budget, rows=[], promotions=promo_rows,
            budget_amount=amount, total_ad_spend=Decimal("0"),
            total_promotions=total_promotions,
            total_committed=total_promotions,
            available=(amount - total_promotions),
            pct_used=((total_promotions / amount * 100).quantize(_CENT) if amount else Decimal("0")),
            days_total=days_total, days_elapsed=0, days_remaining=days_total,
            avg_daily_spend=Decimal("0"), projected_total=total_promotions,
            is_over_budget=(amount - total_promotions) < 0,
            not_started=True, as_of=None,
        )

    as_of = min(budget.end_date, today)
    spend_by_day = _daily_spend(db, budget.start_date, as_of)

    rows: list[AdBudgetDayRow] = []
    committed = Decimal("0")
    total_ad_spend = Decimal("0")
    d = budget.start_date
    while d <= as_of:
        spend = spend_by_day.get(d, Decimal("0"))
        promo = promos_by_day.get(d, Decimal("0"))
        committed += spend + promo
        total_ad_spend += spend
        rows.append(AdBudgetDayRow(
            day=d, ad_spend=spend, promotions=promo,
            committed_to_date=committed, available=(amount - committed),
        ))
        d += timedelta(days=1)

    # Summary uses ALL promotions (including any dated later in the period —
    # entering a promotion is a commitment that offsets available immediately).
    # The daily table shows each promotion on its own date.
    total_committed = total_ad_spend + total_promotions
    available = amount - total_committed
    days_elapsed = (as_of - budget.start_date).days + 1
    days_remaining = max(0, days_total - days_elapsed)
    avg_daily = (total_ad_spend / days_elapsed).quantize(_CENT) if days_elapsed > 0 else Decimal("0")
    projected = (avg_daily * days_total + total_promotions).quantize(_CENT)

    return AdBudgetView(
        budget=budget, rows=rows, promotions=promo_rows,
        budget_amount=amount,
        total_ad_spend=total_ad_spend,
        total_promotions=total_promotions,
        total_committed=total_committed,
        available=available,
        pct_used=((total_committed / amount * 100).quantize(_CENT) if amount else Decimal("0")),
        days_total=days_total, days_elapsed=days_elapsed, days_remaining=days_remaining,
        avg_daily_spend=avg_daily, projected_total=projected,
        is_over_budget=available < 0,
        not_started=False, as_of=as_of,
    )


def list_budgets(db: Session) -> list[AdBudget]:
    """All budgets, newest start first, with promotions eager-loaded."""
    return list(db.execute(
        select(AdBudget)
        .options(selectinload(AdBudget.promotions))
        .order_by(AdBudget.start_date.desc(), AdBudget.id.desc())
    ).scalars().all())


def current_budget(db: Session, *, today: date | None = None) -> AdBudget | None:
    """The budget whose [start, end] contains today; if several overlap, the one
    with the latest start. None when no budget covers today."""
    today = today or today_local()
    return db.execute(
        select(AdBudget)
        .options(selectinload(AdBudget.promotions))
        .where(AdBudget.start_date <= today, AdBudget.end_date >= today)
        .order_by(AdBudget.start_date.desc(), AdBudget.id.desc())
        .limit(1)
    ).scalar_one_or_none()
