"""Sample tracking.

Smashbox has no monthly sample limit, so this report does NOT classify samples
against an allowance, does NOT compute "over-allowance" amounts, and does NOT
treat any unit as a "paid oversample" based on volume. Every shipped sample is
just a shipped sample.

What we report instead:
  - Total samples shipped (count of units).
  - Free vs. explicit-paid breakdown: TikTok's own classification — `SAMPLE`
    when the order is a $0 free creator sample, `PAID_SAMPLE` only if TikTok's
    settlement file explicitly says so (rare; informational only).
  - Status breakdown: how many shipped/delivered/cancelled/etc.
  - Samples sent vs units sold by SKU.
  - Drill-down list of sample orders.

Sources of shipped samples:
  1. Manually-entered rows in the `samples` table (samples NOT sent through
     TikTok Shop).
  2. Orders with `order_type == SAMPLE` (free sample placed via TikTok Shop,
     identified at import by gross_sales == $0).
  3. Orders with `order_type == PAID_SAMPLE` (explicit billed oversamples — rare;
     only set when TikTok's "Sample order type" column says so).
"""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.order import Order, OrderLine, OrderType
from app.models.sample import Sample


@dataclass
class SampleOrderRow:
    placed_at: datetime
    tiktok_order_id: str
    sku: str
    quantity: int
    status: str
    is_paid: bool         # True if TikTok flagged the order as PAID_SAMPLE


@dataclass
class MonthlySampleUsage:
    year: int
    month: int
    brand: str

    free_units_shipped: int             # SAMPLE order_type + Sample table rows
    explicit_paid_units_shipped: int    # PAID_SAMPLE order_type + paid Sample rows
    status_counts: dict[str, int] = field(default_factory=dict)
    sample_orders: list[SampleOrderRow] = field(default_factory=list)

    @property
    def total_units_shipped(self) -> int:
        return self.free_units_shipped + self.explicit_paid_units_shipped


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


def monthly_sample_usage(
    db: Session, year: int, month: int, brand: str | None = None
) -> MonthlySampleUsage:
    brand = brand or settings.default_brand
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

    # Status breakdown across sample-type orders in the window.
    status_rows = db.execute(
        select(Order.status, func.count(Order.id))
        .where(Order.placed_at >= start, Order.placed_at < end)
        .where(Order.order_type.in_([OrderType.SAMPLE, OrderType.PAID_SAMPLE]))
        .group_by(Order.status)
        .order_by(func.count(Order.id).desc())
    ).all()
    status_counts = {(row[0] or "unknown"): int(row[1]) for row in status_rows}

    # Drill-down: one row per OrderLine of a sample order, latest first.
    drill_rows = db.execute(
        select(Order.placed_at, Order.tiktok_order_id, OrderLine.sku,
               OrderLine.quantity, Order.status, Order.order_type)
        .join(OrderLine, OrderLine.order_id == Order.id)
        .where(Order.placed_at >= start, Order.placed_at < end)
        .where(Order.order_type.in_([OrderType.SAMPLE, OrderType.PAID_SAMPLE]))
        .order_by(Order.placed_at.desc())
    ).all()
    sample_orders = [
        SampleOrderRow(
            placed_at=r[0],
            tiktok_order_id=r[1],
            sku=r[2],
            quantity=int(r[3]),
            status=r[4] or "",
            is_paid=(r[5] == OrderType.PAID_SAMPLE),
        )
        for r in drill_rows
    ]

    return MonthlySampleUsage(
        year=year,
        month=month,
        brand=brand,
        free_units_shipped=int(free_units_table) + int(free_units_orders),
        explicit_paid_units_shipped=int(paid_units_table) + int(paid_units_orders),
        status_counts=status_counts,
        sample_orders=sample_orders,
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
