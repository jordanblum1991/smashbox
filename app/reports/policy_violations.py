"""Line-level seller-funded discount policy violations.

The 30%-of-MSRP policy ceiling is enforced PER LINE at import time and
flagged via OrderLine.discount_policy_violation. This report surfaces every
flagged line with the full math so finance can review whether each breach
was intentional.

Definitions (mirror app/rules/seller_funded_split.violates_policy_cap):

  eligible_base   = OrderLine.gross_sales           (MSRP — NOT post-TikTok price)
  cap_amount      = eligible_base × policy_cap_pct  (default 0.30)
  seller_funded   = OrderLine.seller_funded_discount
  pct_of_msrp     = seller_funded / eligible_base   (0 when base is 0)
  excess          = seller_funded − cap_amount      (always > 0 for a violation)

The split invariant still holds when a line breaches the policy: Smashbox
simply absorbs the excess. So the dollars are NOT missing from the P&L —
they're just attributed entirely to Smashbox. This page exists to make those
decisions visible, not to recover funds.

Periods follow the unified PeriodKind from app/reports/pnl.py — MONTH, YTD,
YEAR, RANGE — so the existing period_selector partial drops straight in.
"""
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.bundle import Bundle
from app.models.order import Order, OrderLine, OrderType
from app.models.sku import Sku
from app.services.reporting_tz import now_local, placed_window, today_local
from app.reports.pnl import PeriodKind
from app.templating import month_label


@dataclass
class PolicyViolationRow:
    order_line_id: int
    tiktok_order_id: str
    placed_at: datetime
    status: str
    sku: str
    sku_code: str | None
    name: str | None
    is_bundle: bool
    quantity: int
    gross_sales: Decimal             # MSRP base
    seller_funded_discount: Decimal
    cap_amount: Decimal              # gross × policy_cap_pct
    acknowledged: bool = False
    acknowledged_at: datetime | None = None

    @property
    def excess(self) -> Decimal:
        return self.seller_funded_discount - self.cap_amount

    @property
    def pct_of_msrp(self) -> Decimal:
        if self.gross_sales == 0:
            return Decimal("0")
        return self.seller_funded_discount / self.gross_sales


@dataclass
class PolicyViolationView:
    period_kind: PeriodKind
    year: int
    month: int | None
    title_suffix: str
    start: datetime
    end: datetime
    rows: list[PolicyViolationRow] = field(default_factory=list)
    policy_cap_pct: Decimal = Decimal("0.30")
    monthly_breakdown: list | None = None  # for period_selector compat; unused here

    @property
    def title(self) -> str:
        return f"Policy violations: {self.title_suffix}"

    @property
    def total_excess(self) -> Decimal:
        return sum((r.excess for r in self.rows), Decimal("0"))

    @property
    def total_seller_funded(self) -> Decimal:
        return sum((r.seller_funded_discount for r in self.rows), Decimal("0"))

    @property
    def affected_orders(self) -> int:
        return len({r.tiktok_order_id for r in self.rows})

    @property
    def active_rows(self) -> list["PolicyViolationRow"]:
        return [r for r in self.rows if not r.acknowledged]

    @property
    def acknowledged_rows(self) -> list["PolicyViolationRow"]:
        return [r for r in self.rows if r.acknowledged]


# ---- Period resolver (mirrors PeriodKind from pnl.py) ---------------------

def _first_of_next_month(y: int, m: int) -> datetime:
    return datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1)


def resolve_period(
    period: PeriodKind,
    year: int | None,
    month: int | None,
    start_year: int | None,
    start_month: int | None,
    end_year: int | None,
    end_month: int | None,
    *,
    today: datetime | None = None,
) -> tuple[datetime, datetime, str]:
    now = today or now_local()
    y = year or now.year
    m = month or now.month

    if period == PeriodKind.MONTH:
        return datetime(y, m, 1), _first_of_next_month(y, m), month_label(y, m)

    if period == PeriodKind.YTD:
        return datetime(y, 1, 1), _first_of_next_month(y, m), f"YTD through {month_label(y, m)}"

    if period == PeriodKind.YEAR:
        return datetime(y, 1, 1), datetime(y + 1, 1, 1), str(y)

    sy = start_year or y
    sm = start_month or m
    ey = end_year or y
    em = end_month or m
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

def compute_policy_violations(
    db: Session,
    period: PeriodKind = PeriodKind.MONTH,
    *,
    year: int | None = None,
    month: int | None = None,
    start_year: int | None = None,
    start_month: int | None = None,
    end_year: int | None = None,
    end_month: int | None = None,
) -> PolicyViolationView:
    start, end, suffix = resolve_period(
        period, year, month, start_year, start_month, end_year, end_month
    )
    p_start, p_end = placed_window(start, end)
    cap_pct = settings.seller_funded_policy_cap_pct

    rows_raw = db.execute(
        select(
            OrderLine.id,
            Order.tiktok_order_id,
            Order.placed_at,
            Order.status,
            OrderLine.sku,
            OrderLine.quantity,
            OrderLine.gross_sales,
            OrderLine.seller_funded_discount,
            OrderLine.policy_violation_acknowledged,
            OrderLine.policy_violation_acknowledged_at,
        )
        .join(Order, Order.id == OrderLine.order_id)
        .where(Order.placed_at >= p_start, Order.placed_at < p_end)
        .where(Order.order_type == OrderType.PAID)
        .where(OrderLine.discount_policy_violation.is_(True))
        .order_by(Order.placed_at.desc())
    ).all()

    rows = _build_violation_rows(db, rows_raw, cap_pct)

    today = today_local()
    return PolicyViolationView(
        period_kind=period,
        year=year or today.year,
        month=month or today.month,
        title_suffix=suffix,
        start=start,
        end=end,
        rows=rows,
        policy_cap_pct=cap_pct,
    )


def _build_violation_rows(db: Session, rows_raw, cap_pct: Decimal) -> list[PolicyViolationRow]:
    """Enrich raw (line, order, sku) tuples with catalog name/code and build
    PolicyViolationRow objects. Shared by compute_policy_violations (period-
    scoped) and all_policy_violations (all-time)."""
    keys = {r[4] for r in rows_raw if r[4]}
    sku_by_key: dict[str, Sku] = {}
    bundle_by_key: dict[str, Bundle] = {}
    if keys:
        for s in db.execute(
            select(Sku).where(
                (Sku.tiktok_sku_id.in_(keys)) | (Sku.sku.in_(keys)) | (Sku.tiktok_alt_sku.in_(keys))
            )
        ).scalars():
            for k in (s.tiktok_sku_id, s.sku, s.tiktok_alt_sku):
                if k:
                    sku_by_key.setdefault(str(k), s)
        for b in db.execute(
            select(Bundle).where(
                (Bundle.tiktok_sku_id.in_(keys)) | (Bundle.bundle_sku.in_(keys))
            )
        ).scalars():
            for k in (b.tiktok_sku_id, b.bundle_sku):
                if k:
                    bundle_by_key.setdefault(str(k), b)

    rows: list[PolicyViolationRow] = []
    for line_id, oid, placed, status, sku_key, qty, gross, seller_funded, ack, ack_at in rows_raw:
        g = Decimal(str(gross or 0))
        sf = Decimal(str(seller_funded or 0))
        sku = sku_by_key.get(sku_key)
        bundle = bundle_by_key.get(sku_key)
        if sku:
            name, code, is_bundle = sku.name, sku.sku, False
        elif bundle:
            name, code, is_bundle = bundle.name, bundle.bundle_sku, True
        else:
            name, code, is_bundle = None, None, False

        rows.append(PolicyViolationRow(
            order_line_id=int(line_id),
            tiktok_order_id=oid,
            placed_at=placed,
            status=status or "",
            sku=sku_key,
            sku_code=code,
            name=name,
            is_bundle=is_bundle,
            quantity=int(qty or 0),
            gross_sales=g,
            seller_funded_discount=sf,
            cap_amount=(g * cap_pct).quantize(Decimal("0.01")),
            acknowledged=bool(ack),
            acknowledged_at=ack_at,
        ))
    return rows


def all_policy_violations(db: Session, *, only_unacknowledged: bool = True) -> list[PolicyViolationRow]:
    """All-time policy-violation lines (most recent first), for the Data Health
    overview. only_unacknowledged → exclude lines already acknowledged."""
    cap_pct = settings.seller_funded_policy_cap_pct
    stmt = (
        select(
            OrderLine.id,
            Order.tiktok_order_id,
            Order.placed_at,
            Order.status,
            OrderLine.sku,
            OrderLine.quantity,
            OrderLine.gross_sales,
            OrderLine.seller_funded_discount,
            OrderLine.policy_violation_acknowledged,
            OrderLine.policy_violation_acknowledged_at,
        )
        .join(Order, Order.id == OrderLine.order_id)
        .where(Order.order_type == OrderType.PAID)
        .where(OrderLine.discount_policy_violation.is_(True))
        .order_by(Order.placed_at.desc())
    )
    if only_unacknowledged:
        stmt = stmt.where(OrderLine.policy_violation_acknowledged.is_(False))
    rows_raw = db.execute(stmt).all()
    return _build_violation_rows(db, rows_raw, cap_pct)


def count_policy_violations(db: Session) -> int:
    """All-time count of flagged LINES that have NOT been acknowledged —
    used for the Data Health badge. Acknowledged lines still show on the
    report for audit but don't count toward 'needs attention.'"""
    return int(
        db.execute(
            select(func.count(OrderLine.id))
            .where(OrderLine.discount_policy_violation.is_(True))
            .where(OrderLine.policy_violation_acknowledged.is_(False))
        ).scalar() or 0
    )


def months_with_unacknowledged_violations(db: Session) -> list[tuple[int, int]]:
    """All-time list of (year, month) tuples that contain at least one
    unacknowledged policy-violation line, ordered chronologically.

    Used on the Policy Violations page to nudge the user toward periods
    that still need review — independent of whatever period the selector
    is currently showing."""
    rows = db.execute(
        select(
            func.extract("year", Order.placed_at).label("y"),
            func.extract("month", Order.placed_at).label("m"),
        )
        .join(OrderLine, OrderLine.order_id == Order.id)
        .where(Order.order_type == OrderType.PAID)
        .where(OrderLine.discount_policy_violation.is_(True))
        .where(OrderLine.policy_violation_acknowledged.is_(False))
        .group_by("y", "m")
        .order_by("y", "m")
    ).all()
    return [(int(r.y), int(r.m)) for r in rows]
