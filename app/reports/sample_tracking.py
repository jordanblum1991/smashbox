"""Sample tracking.

Samples come from two sources, unioned in this module:
  1. `samples` table — manually recorded shipments (e.g. samples NOT sent
     through TikTok Shop).
  2. Orders with `order_type == SAMPLE` — free sample orders placed through
     TikTok Shop, detected at import time by gross_sales == $0.

Free units count against the monthly allowance; anything over becomes paid
oversampling.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.order import Order, OrderLine, OrderType
from app.models.sample import Sample


@dataclass
class MonthlySampleUsage:
    year: int
    month: int
    free_units_shipped: int
    paid_oversample_units: int
    allowance: int

    @property
    def units_remaining(self) -> int:
        return max(0, self.allowance - self.free_units_shipped)

    @property
    def over_allowance(self) -> int:
        return max(0, self.free_units_shipped - self.allowance)


@dataclass
class SampleVsSalesRow:
    sku: str
    samples_sent: int
    units_sold: int

    @property
    def ratio(self) -> Decimal:
        if self.samples_sent == 0:
            return Decimal("0")
        return Decimal(self.units_sold) / Decimal(self.samples_sent)


def _month_window(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1)
    end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    return start, end


def monthly_sample_usage(db: Session, year: int, month: int) -> MonthlySampleUsage:
    start, end = _month_window(year, month)

    free_units_table = db.execute(
        select(func.coalesce(func.sum(Sample.quantity), 0))
        .where(Sample.shipped_at >= start, Sample.shipped_at < end)
        .where(Sample.is_paid_oversample.is_(False))
    ).scalar() or 0

    paid_units_table = db.execute(
        select(func.coalesce(func.sum(Sample.quantity), 0))
        .where(Sample.shipped_at >= start, Sample.shipped_at < end)
        .where(Sample.is_paid_oversample.is_(True))
    ).scalar() or 0

    # TikTok $0 orders (OrderType.SAMPLE) — only PAID_SAMPLE counts as paid.
    free_units_orders = db.execute(
        select(func.coalesce(func.sum(OrderLine.quantity), 0))
        .join(Order, Order.id == OrderLine.order_id)
        .where(Order.placed_at >= start, Order.placed_at < end)
        .where(Order.order_type == OrderType.SAMPLE)
    ).scalar() or 0

    paid_units_orders = db.execute(
        select(func.coalesce(func.sum(OrderLine.quantity), 0))
        .join(Order, Order.id == OrderLine.order_id)
        .where(Order.placed_at >= start, Order.placed_at < end)
        .where(Order.order_type == OrderType.PAID_SAMPLE)
    ).scalar() or 0

    return MonthlySampleUsage(
        year=year,
        month=month,
        free_units_shipped=int(free_units_table) + int(free_units_orders),
        paid_oversample_units=int(paid_units_table) + int(paid_units_orders),
        allowance=settings.free_sample_monthly_allowance,
    )


def samples_vs_sales_by_sku(db: Session, start: datetime, end: datetime) -> list[SampleVsSalesRow]:
    samples_table = dict(
        db.execute(
            select(Sample.sku, func.coalesce(func.sum(Sample.quantity), 0))
            .where(Sample.shipped_at >= start, Sample.shipped_at < end)
            .group_by(Sample.sku)
        ).all()
    )
    samples_orders = dict(
        db.execute(
            select(OrderLine.sku, func.coalesce(func.sum(OrderLine.quantity), 0))
            .join(Order, Order.id == OrderLine.order_id)
            .where(Order.placed_at >= start, Order.placed_at < end)
            .where(Order.order_type.in_([OrderType.SAMPLE, OrderType.PAID_SAMPLE]))
            .group_by(OrderLine.sku)
        ).all()
    )
    sales = dict(
        db.execute(
            select(OrderLine.sku, func.coalesce(func.sum(OrderLine.quantity), 0))
            .join(Order, Order.id == OrderLine.order_id)
            .where(Order.order_type == OrderType.PAID)
            .where(Order.placed_at >= start, Order.placed_at < end)
            .group_by(OrderLine.sku)
        ).all()
    )

    skus = sorted(set(samples_table) | set(samples_orders) | set(sales))
    rows = [
        SampleVsSalesRow(
            sku=sku,
            samples_sent=int(samples_table.get(sku, 0)) + int(samples_orders.get(sku, 0)),
            units_sold=int(sales.get(sku, 0)),
        )
        for sku in skus
    ]
    rows.sort(key=lambda r: (-r.samples_sent, r.sku))
    return rows
