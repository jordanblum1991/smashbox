"""Sample tracking.

Smashbox has no monthly sample limit, so this report does NOT classify samples
against an allowance, does NOT compute "over-allowance" amounts, and does NOT
treat any unit as a "paid oversample" based on volume. Every shipped sample is
just a shipped sample.

What we report:
  - Total samples shipped (count of units) over the selected date range.
  - Free vs. explicit-paid breakdown: TikTok's own classification — `SAMPLE`
    when the order is a $0 free creator sample, `PAID_SAMPLE` only when TikTok
    flags it that way (informational only).
  - Status breakdown across all sample orders in the range.
  - Samples sent vs units sold by SKU.
  - Drill-down list of sample-order lines.

Period selection:
  - Single month       (start = first day of M, end = first day of M+1)
  - YTD through month  (start = Jan 1 of Y, end = first day of M+1)
  - Custom range       (start = first day of start_month, end = first day after end_month)

All three modes funnel through one `compute_sample_view()` that operates on a
half-open [start, end) datetime window. No double-counting: every order is
placed at exactly one timestamp and falls into exactly one window.
"""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.bundle import Bundle
from app.models.order import Order, OrderLine, OrderType
from app.models.sample import Sample
from app.models.sku import Sku
from app.templating import month_label


class SamplePeriodKind(str, Enum):
    MONTH = "month"
    YTD = "ytd"
    RANGE = "range"


@dataclass
class SampleOrderRow:
    placed_at: datetime
    tiktok_order_id: str
    sku: str
    quantity: int
    status: str
    is_paid: bool


@dataclass
class SampleVsSalesRow:
    tiktok_sku_id: str       # whatever OrderLine.sku held — usually a TikTok SKU ID
    sku_code: str | None     # SBX-form from catalog; None if unmapped
    name: str | None         # product name from catalog; None if unmapped
    is_bundle: bool          # True when matched to a Bundle row, not a Sku
    samples_sent: int
    units_sold: int

    @property
    def ratio(self) -> Decimal:
        if self.samples_sent == 0:
            return Decimal("0")
        return Decimal(self.units_sold) / Decimal(self.samples_sent)

    @property
    def is_mapped(self) -> bool:
        return self.sku_code is not None or self.name is not None


@dataclass
class ShippedSamplesBySkuRow:
    """One row in the Dashboard 'Samples Sent by SKU' table — actually-shipped only."""
    tiktok_sku_id: str
    sku_code: str | None
    name: str | None
    is_bundle: bool
    samples_sent: int                  # units (sum of quantity)
    sample_orders_shipped: int         # distinct shipped sample order count
    units_sold: int                    # paid units sold (same window)

    @property
    def sold_per_sample(self) -> Decimal:
        if self.samples_sent == 0:
            return Decimal("0")
        return Decimal(self.units_sold) / Decimal(self.samples_sent)

    @property
    def is_unmapped(self) -> bool:
        return self.sku_code is None and self.name is None


# TikTok sample statuses that represent an actual shipment.
# Excluded explicitly: "To ship" (accepted but not yet scanned by carrier),
# and any pending/canceled/withdrawn/unfulfilled/failed value if/when TikTok
# starts emitting them. Manual `Sample` rows always count — each row is a
# recorded ship event.
SHIPPED_SAMPLE_STATUSES = ("Shipped", "Completed")


@dataclass
class SampleView:
    brand: str
    period_kind: SamplePeriodKind
    title_suffix: str          # "May 2026" / "YTD through May 2026" / "March 2026 – May 2026"
    start: datetime
    end: datetime              # half-open

    free_units_shipped: int
    explicit_paid_units_shipped: int
    status_counts: dict[str, int] = field(default_factory=dict)
    sample_orders: list[SampleOrderRow] = field(default_factory=list)
    by_sku: list[SampleVsSalesRow] = field(default_factory=list)

    @property
    def total_units_shipped(self) -> int:
        return self.free_units_shipped + self.explicit_paid_units_shipped

    @property
    def title(self) -> str:
        return f"Sample Tracking: {self.title_suffix}"


# ---- Period -> window resolver --------------------------------------------

def _first_of_next_month(y: int, m: int) -> datetime:
    return datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1)


def resolve_period(
    period: SamplePeriodKind,
    year: int | None,
    month: int | None,
    start_year: int | None,
    start_month: int | None,
    end_year: int | None,
    end_month: int | None,
    *,
    today: datetime | None = None,
) -> tuple[datetime, datetime, str]:
    """Return (start, end, title_suffix). End is exclusive (first of next month)."""
    now = today or datetime.now()
    y = year or now.year
    m = month or now.month

    if period == SamplePeriodKind.MONTH:
        start = datetime(y, m, 1)
        end = _first_of_next_month(y, m)
        return start, end, month_label(y, m)

    if period == SamplePeriodKind.YTD:
        start = datetime(y, 1, 1)
        end = _first_of_next_month(y, m)
        return start, end, f"YTD through {month_label(y, m)}"

    # RANGE — fall back to single-month-of-now if any field missing
    sy = start_year or y
    sm = start_month or m
    ey = end_year or y
    em = end_month or m
    # Allow user to pick end < start by silently swapping (less surprising than 500ing).
    if (ey, em) < (sy, sm):
        sy, sm, ey, em = ey, em, sy, sm
    start = datetime(sy, sm, 1)
    end = _first_of_next_month(ey, em)
    if (sy, sm) == (ey, em):
        suffix = month_label(sy, sm)
    else:
        suffix = f"{month_label(sy, sm)} – {month_label(ey, em)}"
    return start, end, suffix


# ---- Main computation -----------------------------------------------------

def compute_sample_view(
    db: Session,
    period: SamplePeriodKind,
    *,
    year: int | None = None,
    month: int | None = None,
    start_year: int | None = None,
    start_month: int | None = None,
    end_year: int | None = None,
    end_month: int | None = None,
    brand: str | None = None,
) -> SampleView:
    brand = brand or settings.default_brand
    start, end, suffix = resolve_period(
        period, year, month, start_year, start_month, end_year, end_month
    )

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

    status_rows = db.execute(
        select(Order.status, func.count(Order.id))
        .where(Order.placed_at >= start, Order.placed_at < end)
        .where(Order.order_type.in_([OrderType.SAMPLE, OrderType.PAID_SAMPLE]))
        .group_by(Order.status)
        .order_by(func.count(Order.id).desc())
    ).all()
    status_counts = {(row[0] or "unknown"): int(row[1]) for row in status_rows}

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

    by_sku = samples_vs_sales_by_sku(db, start, end)

    return SampleView(
        brand=brand,
        period_kind=period,
        title_suffix=suffix,
        start=start,
        end=end,
        free_units_shipped=int(free_units_table) + int(free_units_orders),
        explicit_paid_units_shipped=int(paid_units_table) + int(paid_units_orders),
        status_counts=status_counts,
        sample_orders=sample_orders,
        by_sku=by_sku,
    )


def samples_by_sku_shipped(
    db: Session, start: datetime, end: datetime
) -> list[ShippedSamplesBySkuRow]:
    """Per-SKU rollup of ACTUALLY SHIPPED samples in [start, end), with paid units
    sold over the same window for ratio analysis.

    Order rows count only when Order.status ∈ SHIPPED_SAMPLE_STATUSES — pending,
    canceled, withdrawn, unfulfilled, failed, and 'To ship' rows are excluded.
    Manual Sample-table rows always count (each row is a recorded ship event).
    Only SKUs with at least one shipped sample appear — pure-sales SKUs aren't
    listed (this section is about samples).
    """
    samples_table_units = dict(
        db.execute(
            select(Sample.sku, func.coalesce(func.sum(Sample.quantity), 0))
            .where(Sample.shipped_at >= start, Sample.shipped_at < end)
            .group_by(Sample.sku)
        ).all()
    )
    samples_table_orders = dict(
        db.execute(
            select(Sample.sku, func.count(Sample.id))
            .where(Sample.shipped_at >= start, Sample.shipped_at < end)
            .group_by(Sample.sku)
        ).all()
    )
    samples_orders_units = dict(
        db.execute(
            select(OrderLine.sku, func.coalesce(func.sum(OrderLine.quantity), 0))
            .join(Order, Order.id == OrderLine.order_id)
            .where(Order.placed_at >= start, Order.placed_at < end)
            .where(Order.order_type.in_([OrderType.SAMPLE, OrderType.PAID_SAMPLE]))
            .where(Order.status.in_(SHIPPED_SAMPLE_STATUSES))
            .group_by(OrderLine.sku)
        ).all()
    )
    samples_orders_count = dict(
        db.execute(
            select(OrderLine.sku, func.count(func.distinct(Order.id)))
            .join(Order, Order.id == OrderLine.order_id)
            .where(Order.placed_at >= start, Order.placed_at < end)
            .where(Order.order_type.in_([OrderType.SAMPLE, OrderType.PAID_SAMPLE]))
            .where(Order.status.in_(SHIPPED_SAMPLE_STATUSES))
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

    skus = sorted(set(samples_table_units) | set(samples_orders_units))
    if not skus:
        return []

    sku_by_key: dict[str, Sku] = {}
    for s in db.execute(
        select(Sku).where(
            (Sku.tiktok_sku_id.in_(skus))
            | (Sku.sku.in_(skus))
            | (Sku.tiktok_alt_sku.in_(skus))
        )
    ).scalars():
        for key in (s.tiktok_sku_id, s.sku, s.tiktok_alt_sku):
            if key:
                sku_by_key[str(key)] = s

    bundle_by_key: dict[str, Bundle] = {}
    for b in db.execute(
        select(Bundle).where(
            (Bundle.tiktok_sku_id.in_(skus)) | (Bundle.bundle_sku.in_(skus))
        )
    ).scalars():
        for key in (b.tiktok_sku_id, b.bundle_sku):
            if key:
                bundle_by_key[str(key)] = b

    rows: list[ShippedSamplesBySkuRow] = []
    for raw_key in skus:
        sku = sku_by_key.get(raw_key)
        bundle = bundle_by_key.get(raw_key)
        if sku:
            name, code, is_bundle = sku.name, sku.sku, False
        elif bundle:
            name, code, is_bundle = bundle.name, bundle.bundle_sku, True
        else:
            name, code, is_bundle = None, None, False
        rows.append(ShippedSamplesBySkuRow(
            tiktok_sku_id=raw_key,
            sku_code=code,
            name=name,
            is_bundle=is_bundle,
            samples_sent=int(samples_table_units.get(raw_key, 0)) + int(samples_orders_units.get(raw_key, 0)),
            sample_orders_shipped=int(samples_table_orders.get(raw_key, 0)) + int(samples_orders_count.get(raw_key, 0)),
            units_sold=int(sales.get(raw_key, 0)),
        ))
    rows.sort(key=lambda r: (-r.samples_sent, r.tiktok_sku_id))
    return rows


def count_samples_shipped(db: Session, start: datetime, end: datetime) -> int:
    """Total units shipped as samples in [start, end). Same definition the Sample
    Tracking page uses (free + explicit-paid), but a single integer for tiles."""
    from_sample_table = db.execute(
        select(func.coalesce(func.sum(Sample.quantity), 0))
        .where(Sample.shipped_at >= start, Sample.shipped_at < end)
    ).scalar() or 0
    from_orders = db.execute(
        select(func.coalesce(func.sum(OrderLine.quantity), 0))
        .join(Order, Order.id == OrderLine.order_id)
        .where(Order.placed_at >= start, Order.placed_at < end)
        .where(Order.order_type.in_([OrderType.SAMPLE, OrderType.PAID_SAMPLE]))
    ).scalar() or 0
    return int(from_sample_table) + int(from_orders)


def samples_vs_sales_by_sku(db: Session, start: datetime, end: datetime) -> list[SampleVsSalesRow]:
    """Aggregate samples sent + units sold per SKU across [start, end).

    GROUP BY OrderLine.sku ensures each (order, SKU) line contributes exactly
    once — no double counting even when an order has multiple line items or
    when the same SKU appears across many orders in the window.

    Each output row is enriched with the catalog SKU code and product name
    (Sku table first, Bundle table fallback). Unmapped rows return sku_code
    and name as None so the template can render "Missing …" labels.
    """
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
    if not skus:
        return []

    # One catalog fetch each (Sku, Bundle) — N+1 free.
    sku_by_key: dict[str, Sku] = {}
    for s in db.execute(
        select(Sku).where(
            (Sku.tiktok_sku_id.in_(skus))
            | (Sku.sku.in_(skus))
            | (Sku.tiktok_alt_sku.in_(skus))
        )
    ).scalars():
        for key in (s.tiktok_sku_id, s.sku, s.tiktok_alt_sku):
            if key:
                sku_by_key[str(key)] = s

    bundle_by_key: dict[str, Bundle] = {}
    for b in db.execute(
        select(Bundle).where(
            (Bundle.tiktok_sku_id.in_(skus)) | (Bundle.bundle_sku.in_(skus))
        )
    ).scalars():
        for key in (b.tiktok_sku_id, b.bundle_sku):
            if key:
                bundle_by_key[str(key)] = b

    rows: list[SampleVsSalesRow] = []
    for raw_key in skus:
        sku = sku_by_key.get(raw_key)
        bundle = bundle_by_key.get(raw_key)
        if sku:
            name, code, is_bundle = sku.name, sku.sku, False
        elif bundle:
            name, code, is_bundle = bundle.name, bundle.bundle_sku, True
        else:
            name, code, is_bundle = None, None, False

        rows.append(SampleVsSalesRow(
            tiktok_sku_id=raw_key,
            sku_code=code,
            name=name,
            is_bundle=is_bundle,
            samples_sent=int(samples_table.get(raw_key, 0)) + int(samples_orders.get(raw_key, 0)),
            units_sold=int(sales.get(raw_key, 0)),
        ))
    rows.sort(key=lambda r: (-r.samples_sent, r.tiktok_sku_id))
    return rows
