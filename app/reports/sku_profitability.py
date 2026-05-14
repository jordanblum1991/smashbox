"""SKU-level profitability.

OrderLine.sku is canonicalized to the TikTok SKU ID by the resolver, so reports
group on it and join the catalog (Sku.tiktok_sku_id and Bundle.tiktok_sku_id)
to enrich each row with a human-readable name and the SBX-form short code.

TODO: bundle explosion — currently a bundle sale contributes to the bundle's
TikTok SKU ID. To attribute to component SKUs, post-process the rows by
expanding via BundleComponent (proportional to component MSRP or COGS).
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


def compute_sku_profitability(db: Session, start: datetime, end: datetime) -> list[SkuRow]:
    """One row per OrderLine.sku in the window; enriched from Sku / Bundle."""
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
        .order_by(func.sum(OrderLine.gross_sales).desc())
    )
    raw = list(db.execute(agg))
    if not raw:
        return []

    # Enrich via two cheap lookups so we don't N+1.
    keys = [r.key for r in raw if r.key]
    sku_rows = db.execute(
        select(Sku).where(
            (Sku.tiktok_sku_id.in_(keys)) | (Sku.sku.in_(keys)) | (Sku.tiktok_alt_sku.in_(keys))
        )
    ).scalars().all()
    sku_by_key = {}
    for s in sku_rows:
        for k in (s.tiktok_sku_id, s.sku, s.tiktok_alt_sku):
            if k:
                sku_by_key[str(k)] = s

    bundle_rows = db.execute(
        select(Bundle).where(
            (Bundle.tiktok_sku_id.in_(keys)) | (Bundle.bundle_sku.in_(keys))
        )
    ).scalars().all()
    bundle_by_key = {}
    for b in bundle_rows:
        for k in (b.tiktok_sku_id, b.bundle_sku):
            if k:
                bundle_by_key[str(k)] = b

    out: list[SkuRow] = []
    for r in raw:
        key = str(r.key)
        gross = Decimal(str(r.gross))
        cogs = Decimal(str(r.cogs))
        sku = sku_by_key.get(key)
        bundle = bundle_by_key.get(key)
        if sku:
            name, code, is_bundle = sku.name, sku.sku, False
        elif bundle:
            name, code, is_bundle = bundle.name, bundle.bundle_sku, True
        else:
            name, code, is_bundle = None, None, False

        out.append(SkuRow(
            tiktok_sku_id=key,
            sku_code=code,
            name=name,
            is_bundle=is_bundle,
            units_sold=int(r.units),
            gross_sales=gross,
            cogs=cogs,
            gross_profit=gross - cogs,
        ))
    return out
