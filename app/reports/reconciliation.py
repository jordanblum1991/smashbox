"""Reconciliation — does what we computed match what TikTok says?

For a given month, compare our derived totals against the raw settlement /
payout rows TikTok provided. Any non-zero variance means an importer or
business rule needs investigating.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.order import Order, OrderType
from app.models.payout import Payout
from app.models.settlement import Settlement


@dataclass
class ReconciliationLine:
    label: str
    derived: Decimal      # what we computed from order/line tables
    tiktok: Decimal       # what TikTok's settlement/payout export says
    tolerance_cents: int = 1

    @property
    def variance(self) -> Decimal:
        return self.derived - self.tiktok

    @property
    def ok(self) -> bool:
        return abs(self.variance) <= Decimal(self.tolerance_cents) / Decimal(100)


def reconcile_month(db: Session, year: int, month: int) -> list[ReconciliationLine]:
    start = datetime(year, month, 1)
    end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)

    derived_gross = db.execute(
        select(func.coalesce(func.sum(Order.gross_sales), 0))
        .where(Order.placed_at >= start, Order.placed_at < end)
        .where(Order.order_type == OrderType.PAID)
    ).scalar() or 0

    tiktok_sales = db.execute(
        select(func.coalesce(func.sum(Settlement.amount), 0))
        .where(Settlement.settled_at >= start, Settlement.settled_at < end)
        .where(Settlement.event_type == "sale")
    ).scalar() or 0

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

    tiktok_payout_net = db.execute(
        select(func.coalesce(func.sum(Payout.net_amount), 0))
        .where(Payout.paid_at >= start, Payout.paid_at < end)
    ).scalar() or 0

    return [
        ReconciliationLine(
            label="Gross sales (paid orders) vs settlement file",
            derived=Decimal(str(derived_gross)),
            tiktok=Decimal(str(tiktok_sales)),
        ),
        ReconciliationLine(
            label="Seller-funded split (Outlandish + Smashbox) vs TikTok total",
            derived=Decimal(str(derived_sf)),
            tiktok=Decimal(str(tiktok_sf)),
            tolerance_cents=0,  # this MUST be exact — see app/rules/seller_funded_split.py
        ),
        ReconciliationLine(
            label="Payouts (informational — net cash received this month)",
            derived=Decimal("0"),
            tiktok=Decimal(str(tiktok_payout_net)),
        ),
    ]
