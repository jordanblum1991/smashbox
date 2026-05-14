"""Sample tracking.

Sources of shipped samples:
  1. Manually-entered rows in the `samples` table (samples NOT sent through
     TikTok Shop).
  2. Orders with `order_type == SAMPLE` (free sample placed via TikTok Shop,
     identified at import by gross_sales == $0).
  3. Orders with `order_type == PAID_SAMPLE` (explicit billed oversamples — rare;
     only set when TikTok's "Sample order type" column says so).

Free-vs-paid classification:
  free_used        = MIN(total_free_units_shipped, allowance)
  auto_oversample  = MAX(0, total_free_units_shipped − allowance)
  paid_oversamples = explicit_paid_units + auto_oversample
  remaining        = MAX(0, allowance − total_free_units_shipped)

The allowance comes from the `sample_allowances` table (brand, year, month).
If no row exists for that period, we fall back to `settings.free_sample_monthly_allowance`
and surface "(fallback default)" as the source so the user knows.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.order import Order, OrderLine, OrderType
from app.models.sample import Sample
from app.models.sample_allowance import SampleAllowance


@dataclass
class MonthlySampleUsage:
    year: int
    month: int
    brand: str

    total_free_units_shipped: int    # units shipped as 'free' (Order.SAMPLE + Sample table free)
    explicit_paid_units: int          # explicitly billed (Order.PAID_SAMPLE + Sample.is_paid_oversample)
    allowance: int
    allowance_source: str             # human-readable provenance
    allowance_rule_id: int | None     # SampleAllowance.id if a rule matched

    @property
    def auto_oversample_units(self) -> int:
        return max(0, self.total_free_units_shipped - self.allowance)

    @property
    def free_units_used(self) -> int:
        return min(self.total_free_units_shipped, self.allowance)

    @property
    def paid_oversample_units(self) -> int:
        """Total billed-as-paid: explicit + automatic over-allowance."""
        return self.explicit_paid_units + self.auto_oversample_units

    @property
    def units_remaining(self) -> int:
        return max(0, self.allowance - self.total_free_units_shipped)

    @property
    def total_units_shipped(self) -> int:
        return self.total_free_units_shipped + self.explicit_paid_units


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


def _resolve_allowance(
    db: Session, brand: str, year: int, month: int
) -> tuple[int, str, int | None]:
    """Look up the allowance for (brand, year, month).

    Returns (units, source_description, rule_id_or_none).
    """
    rule = db.execute(
        select(SampleAllowance)
        .where(SampleAllowance.brand == brand)
        .where(SampleAllowance.year == year)
        .where(SampleAllowance.month == month)
    ).scalar_one_or_none()

    if rule is not None:
        note_part = f" — “{rule.notes}”" if rule.notes else ""
        return (
            rule.allowance_units,
            f"sample_allowances rule #{rule.id} ({rule.brand}, "
            f"{year}-{month:02d}, {rule.allowance_units} units){note_part}",
            rule.id,
        )

    return (
        settings.free_sample_monthly_allowance,
        f"fallback default (no rule in sample_allowances for "
        f"{brand} {year}-{month:02d}; env FREE_SAMPLE_MONTHLY_ALLOWANCE="
        f"{settings.free_sample_monthly_allowance})",
        None,
    )


def monthly_sample_usage(
    db: Session, year: int, month: int, brand: str | None = None
) -> MonthlySampleUsage:
    brand = brand or settings.default_brand
    start, end = _month_window(year, month)

    # 1. Manually-recorded samples
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

    # 2. TikTok $0 free-sample orders
    free_units_orders = db.execute(
        select(func.coalesce(func.sum(OrderLine.quantity), 0))
        .join(Order, Order.id == OrderLine.order_id)
        .where(Order.placed_at >= start, Order.placed_at < end)
        .where(Order.order_type == OrderType.SAMPLE)
    ).scalar() or 0

    # 3. Explicit paid-sample orders
    paid_units_orders = db.execute(
        select(func.coalesce(func.sum(OrderLine.quantity), 0))
        .join(Order, Order.id == OrderLine.order_id)
        .where(Order.placed_at >= start, Order.placed_at < end)
        .where(Order.order_type == OrderType.PAID_SAMPLE)
    ).scalar() or 0

    allowance, source, rule_id = _resolve_allowance(db, brand, year, month)

    return MonthlySampleUsage(
        year=year,
        month=month,
        brand=brand,
        total_free_units_shipped=int(free_units_table) + int(free_units_orders),
        explicit_paid_units=int(paid_units_table) + int(paid_units_orders),
        allowance=allowance,
        allowance_source=source,
        allowance_rule_id=rule_id,
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


def list_allowance_rules(db: Session) -> list[SampleAllowance]:
    return list(
        db.execute(
            select(SampleAllowance).order_by(
                SampleAllowance.brand, SampleAllowance.year, SampleAllowance.month
            )
        ).scalars()
    )
