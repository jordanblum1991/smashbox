"""SKU-level profitability.

OrderLine.sku is canonicalized to the TikTok SKU ID by the resolver, so reports
group on it and join the catalog (Sku.tiktok_sku_id and Bundle.tiktok_sku_id)
to enrich each row with a human-readable name and the SBX-form short code.

Bundle explosion (sku-profitability only)
-----------------------------------------
A bundle sale carries the bundle's TikTok SKU ID on its OrderLine, so the SQL
aggregation initially groups it as a single bundle row. To reflect what
physical inventory actually moved, we POST-PROCESS bundle rows by walking
their BundleComponent entries and reallocating units / gross / COGS to each
component SKU. Allocation rules per OrderLine sale of bundle B (gross G,
quantity Q, unit_cogs_snapshot K):

    units(C)  = Q × C.quantity                        (physical units shipped)
    cogs(C)   = Q × C.quantity × C.unit_cogs          (this component's COGS)
    gross(C)  = G × (C.quantity × C.unit_cogs) / K    (share of bundle gross)

The COGS and gross sums across components exactly equal the bundle line's
COGS and gross — by construction. When K is zero (bundle has no COGS info),
fall back to allocating by component MSRP. If MSRP is also zero, the bundle
row is left as-is (can't allocate).

`compute_top_skus` deliberately does NOT explode. That panel ranks "products
people bought" — a bundle is a discrete sellable item; splitting it would
muddle the message ("most-sold product").
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.bundle import Bundle
from app.models.order import Order, OrderLine, OrderType
from app.models.sku import Sku


@dataclass
class SkuRow:
    tiktok_sku_id: str
    sku_code: str | None         # SBX-form when known
    name: str | None
    is_bundle: bool
    units_sold: int
    gross_sales: Decimal
    cogs: Decimal
    gross_profit: Decimal

    @property
    def gross_margin(self) -> Decimal:
        if self.gross_sales == 0:
            return Decimal("0")
        return self.gross_profit / self.gross_sales


@dataclass
class TopSkuRow:
    """One entry in the Dashboard's Top 10 Best Sellers table."""
    rank: int
    tiktok_sku_id: str
    sku_code: str | None
    name: str | None
    is_bundle: bool
    units_sold: int
    net_customer_sales: Decimal
    orders: int

    @property
    def aov(self) -> Decimal:
        if self.orders == 0:
            return Decimal("0")
        return self.net_customer_sales / Decimal(self.orders)

    @property
    def is_unmapped(self) -> bool:
        return self.sku_code is None and self.name is None


def compute_sku_profitability(db: Session, start: datetime, end: datetime) -> list[SkuRow]:
    """One row per physical SKU in the window — bundles are exploded into
    their components. Enriched with name/SBX-form code from the catalog."""
    agg = (
        select(
            OrderLine.sku.label("key"),
            func.coalesce(func.sum(OrderLine.quantity), 0).label("units"),
            func.coalesce(func.sum(OrderLine.gross_sales), 0).label("gross"),
            func.coalesce(
                func.sum(
                    OrderLine.quantity
                    * func.coalesce(func.nullif(OrderLine.unit_cogs_snapshot, 0), 0)
                ),
                0,
            ).label("cogs"),
        )
        .select_from(OrderLine)
        .join(Order, Order.id == OrderLine.order_id)
        .where(Order.order_type == OrderType.PAID)
        .where(Order.placed_at >= start, Order.placed_at < end)
        .group_by(OrderLine.sku)
    )
    raw = list(db.execute(agg))
    if not raw:
        return []

    keys = [r.key for r in raw if r.key]
    sku_by_key = _load_sku_index(db, keys)
    bundle_by_key = _load_bundle_index(db, keys)
    # Pre-load Sku rows for every bundle component so explosion can resolve
    # each component to its canonical TikTok SKU ID in one query.
    comp_sku_by_sbx = _load_component_sku_index(db, bundle_by_key.values())

    # Accumulate into a dict keyed by *physical* SKU. A given component might
    # appear both as a direct sale AND inside a bundle in the same period;
    # those land in the same bucket here.
    physical: dict[str, _Acc] = {}

    for r in raw:
        key = str(r.key)
        units = int(r.units)
        gross = Decimal(str(r.gross))
        cogs = Decimal(str(r.cogs))
        sku = sku_by_key.get(key)
        bundle = bundle_by_key.get(key)

        if bundle is not None and bundle.components and _can_explode(bundle, cogs):
            _explode_bundle(physical, bundle, units, gross, cogs, comp_sku_by_sbx)
        elif sku:
            _add(physical, key, units, gross, cogs, sku.name, sku.sku, is_bundle=False)
        elif bundle:
            # Bundle with no allocable basis — leave as-is so its dollars aren't lost.
            _add(
                physical, key, units, gross, cogs,
                bundle.name, bundle.bundle_sku, is_bundle=True,
            )
        else:
            _add(physical, key, units, gross, cogs, None, None, is_bundle=False)

    out = [
        SkuRow(
            tiktok_sku_id=acc.key,
            sku_code=acc.code,
            name=acc.name,
            is_bundle=acc.is_bundle,
            units_sold=acc.units,
            gross_sales=acc.gross,
            cogs=acc.cogs,
            gross_profit=acc.gross - acc.cogs,
        )
        for acc in physical.values()
    ]
    out.sort(key=lambda r: r.gross_sales, reverse=True)
    return out


# ---- Bundle explosion machinery -------------------------------------------

@dataclass
class _Acc:
    key: str
    units: int = 0
    gross: Decimal = Decimal("0")
    cogs: Decimal = Decimal("0")
    name: str | None = None
    code: str | None = None
    is_bundle: bool = False


def _add(
    physical: dict[str, _Acc],
    key: str,
    units: int,
    gross: Decimal,
    cogs: Decimal,
    name: str | None,
    code: str | None,
    *,
    is_bundle: bool,
) -> None:
    acc = physical.get(key)
    if acc is None:
        acc = _Acc(key=key, name=name, code=code, is_bundle=is_bundle)
        physical[key] = acc
    acc.units += units
    acc.gross += gross
    acc.cogs += cogs
    # First-seen wins for naming — direct-sale rows tend to surface enrichment
    # earlier in the iteration than bundle-component allocations.
    if not acc.name and name:
        acc.name = name
    if not acc.code and code:
        acc.code = code


def _can_explode(bundle: Bundle, line_cogs: Decimal) -> bool:
    """A bundle is explodable when we have a non-zero basis to allocate by."""
    cogs_basis = sum(
        (c.quantity * c.unit_cogs for c in bundle.components), Decimal("0")
    )
    if cogs_basis > 0:
        return True
    msrp_basis = sum(
        (c.quantity * c.msrp for c in bundle.components), Decimal("0")
    )
    return msrp_basis > 0


def _explode_bundle(
    physical: dict[str, _Acc],
    bundle: Bundle,
    line_units: int,
    line_gross: Decimal,
    line_cogs: Decimal,
    comp_sku_by_sbx: dict[str, Sku],
) -> None:
    """Reallocate the bundle line's units/gross/COGS to its components.

    Uses component COGS share as the allocation basis; falls back to MSRP
    when COGS data is missing. Caller has already verified at least one basis
    is non-zero via _can_explode().
    """
    cogs_basis = sum(
        (c.quantity * c.unit_cogs for c in bundle.components), Decimal("0")
    )
    if cogs_basis > 0:
        basis_total = cogs_basis
        basis_fn = lambda c: c.quantity * c.unit_cogs  # noqa: E731
    else:
        basis_total = sum(
            (c.quantity * c.msrp for c in bundle.components), Decimal("0")
        )
        basis_fn = lambda c: c.quantity * c.msrp  # noqa: E731

    for c in bundle.components:
        comp_sku = comp_sku_by_sbx.get(c.component_sku) if c.component_sku else None
        if comp_sku and comp_sku.tiktok_sku_id:
            comp_key = comp_sku.tiktok_sku_id
        elif comp_sku and comp_sku.sku:
            comp_key = comp_sku.sku
        else:
            comp_key = c.component_sku or f"bundle-{bundle.id}-component"
        comp_name = (comp_sku.name if comp_sku else None) or c.component_name
        comp_code = (comp_sku.sku if comp_sku else None) or c.component_sku

        c_basis = basis_fn(c)
        # gross share is proportional to allocation basis;
        # cogs and units fall straight out of the component definition.
        c_gross = (line_gross * c_basis / basis_total) if basis_total else Decimal("0")
        c_cogs = Decimal(line_units) * c.quantity * c.unit_cogs
        c_units = line_units * c.quantity

        _add(physical, comp_key, c_units, c_gross, c_cogs, comp_name, comp_code, is_bundle=False)


def _load_sku_index(db: Session, keys: list[str]) -> dict[str, Sku]:
    if not keys:
        return {}
    out: dict[str, Sku] = {}
    for s in db.execute(
        select(Sku).where(
            (Sku.tiktok_sku_id.in_(keys)) | (Sku.sku.in_(keys)) | (Sku.tiktok_alt_sku.in_(keys))
        )
    ).scalars():
        for k in (s.tiktok_sku_id, s.sku, s.tiktok_alt_sku):
            if k:
                out.setdefault(str(k), s)
    return out


def _load_bundle_index(db: Session, keys: list[str]) -> dict[str, Bundle]:
    if not keys:
        return {}
    out: dict[str, Bundle] = {}
    for b in db.execute(
        select(Bundle).where(
            (Bundle.tiktok_sku_id.in_(keys)) | (Bundle.bundle_sku.in_(keys))
        )
    ).scalars():
        for k in (b.tiktok_sku_id, b.bundle_sku):
            if k:
                out.setdefault(str(k), b)
    return out


def _load_component_sku_index(db: Session, bundles) -> dict[str, Sku]:
    """Map BundleComponent.component_sku (SBX-form) → Sku row.

    A given SBX-form code can map to multiple Sku rows (one per TikTok
    variation). First seen wins — variations of the same physical product
    share name and COGS so any is fine for explosion purposes.
    """
    sbx_codes = {c.component_sku for b in bundles for c in b.components if c.component_sku}
    if not sbx_codes:
        return {}
    out: dict[str, Sku] = {}
    for s in db.execute(select(Sku).where(Sku.sku.in_(sbx_codes))).scalars():
        if s.sku:
            out.setdefault(str(s.sku), s)
    return out


def compute_top_skus(
    db: Session, start: datetime, end: datetime, limit: int = 10
) -> list[TopSkuRow]:
    """Top-N SKUs by units sold in [start, end). PAID orders only.

    A bundle counts as 1 unit per OrderLine — bundles are NOT exploded into
    component SKUs (matches the SKU-profitability convention).

    Net Customer Sales is computed at the LINE level:
        gross_sales − platform_discount − seller_funded_outlandish − seller_funded_smashbox
    Order-grain refunds are excluded — they can't be cleanly attributed to a
    specific line. The dashboard NCS tile (which includes refunds) is therefore
    not directly comparable to the SUM of this table's NCS column.

    AOV is Net Customer Sales / DISTINCT orders containing this SKU.
    """
    agg = (
        select(
            OrderLine.sku.label("key"),
            func.coalesce(func.sum(OrderLine.quantity), 0).label("units"),
            func.coalesce(
                func.sum(
                    OrderLine.gross_sales
                    - OrderLine.platform_discount
                    - OrderLine.seller_funded_outlandish
                    - OrderLine.seller_funded_smashbox
                ),
                0,
            ).label("net"),
            func.count(func.distinct(Order.id)).label("orders"),
        )
        .select_from(OrderLine)
        .join(Order, Order.id == OrderLine.order_id)
        .where(Order.order_type == OrderType.PAID)
        .where(Order.placed_at >= start, Order.placed_at < end)
        .group_by(OrderLine.sku)
        .order_by(func.sum(OrderLine.quantity).desc())
        .limit(limit)
    )
    raw = list(db.execute(agg))
    if not raw:
        return []

    keys = [r.key for r in raw if r.key]
    sku_by_key = _load_sku_index(db, keys)
    bundle_by_key = _load_bundle_index(db, keys)

    out: list[TopSkuRow] = []
    for rank, r in enumerate(raw, start=1):
        key = str(r.key)
        sku = sku_by_key.get(key)
        bundle = bundle_by_key.get(key)
        if sku:
            name, code, is_bundle = sku.name, sku.sku, False
        elif bundle:
            name, code, is_bundle = bundle.name, bundle.bundle_sku, True
        else:
            name, code, is_bundle = None, None, False
        out.append(TopSkuRow(
            rank=rank,
            tiktok_sku_id=key,
            sku_code=code,
            name=name,
            is_bundle=is_bundle,
            units_sold=int(r.units),
            net_customer_sales=Decimal(str(r.net)),
            orders=int(r.orders),
        ))
    return out
