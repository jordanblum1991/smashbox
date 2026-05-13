"""TikTok Shop orders export importer.

The actual column names in TikTok's export change periodically — keep the
header map at the top of this file and adjust when TikTok updates the format.
A real fixture should be added to tests/fixtures/tiktok_orders_sample.xlsx once
we have one.
"""
from decimal import Decimal
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

from app.config import settings
from app.importers.base import BaseImporter, ImportResult
from app.models.import_batch import ImportBatch
from app.models.order import Order, OrderLine, OrderType
from app.rules.seller_funded_split import split_seller_funded_discount

# Map "what we call it" -> "what TikTok calls it in the export header".
# Update this when TikTok renames a column instead of editing read logic below.
HEADER_MAP = {
    "tiktok_order_id": "Order ID",
    "placed_at": "Created Time",
    "status": "Order Status",
    "sku": "Seller SKU",
    "quantity": "Quantity",
    "unit_price": "SKU Unit Original Price",
    "gross_sales": "SKU Subtotal Before Discount",
    "seller_funded_discount": "Seller Discount",
    "shipping_revenue": "Shipping Fee After Discount",
    "is_sample": "Free Sample",  # if/when TikTok marks free samples
}


class TikTokOrdersImporter(BaseImporter):
    def run(self, path: Path, db: Session, batch: ImportBatch) -> ImportResult:
        result = ImportResult()

        df = _read_any(path)
        df = _normalize_headers(df)

        # Group by order — TikTok exports one row per line item.
        for tiktok_order_id, group in df.groupby("tiktok_order_id"):
            try:
                order = _build_order(tiktok_order_id, group, batch)
                db.add(order)
                result.rows_imported += 1
            except Exception as exc:  # noqa: BLE001
                result.skip(f"order {tiktok_order_id}: {exc}")

        return result


def _read_any(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(path)
    if suffix == ".csv":
        return pd.read_csv(path)
    raise ValueError(f"unsupported file type: {suffix}")


def _normalize_headers(df: pd.DataFrame) -> pd.DataFrame:
    """Rename TikTok headers to our internal names. Unknown columns are kept."""
    reverse = {v: k for k, v in HEADER_MAP.items()}
    return df.rename(columns=reverse)


def _build_order(tiktok_order_id: str, group: pd.DataFrame, batch: ImportBatch) -> Order:
    first = group.iloc[0]
    is_sample = bool(first.get("is_sample", False))
    order_type = OrderType.SAMPLE if is_sample else OrderType.PAID

    gross_sales = _sum_decimal(group, "gross_sales")
    seller_funded_total = _sum_decimal(group, "seller_funded_discount")
    split = split_seller_funded_discount(seller_funded_total)

    order = Order(
        import_batch_id=batch.id,
        tiktok_order_id=str(tiktok_order_id),
        placed_at=pd.to_datetime(first["placed_at"]).to_pydatetime(),
        order_type=order_type,
        status=str(first.get("status", "unknown")),
        brand=settings.default_brand,
        gross_sales=gross_sales,
        shipping_revenue=_sum_decimal(group, "shipping_revenue"),
        seller_funded_discount_total=split.total,
        seller_funded_outlandish=split.outlandish,
        seller_funded_smashbox=split.smashbox,
    )
    order.lines = [
        OrderLine(
            sku=str(row["sku"]),
            quantity=int(row.get("quantity", 1)),
            unit_price=_dec(row.get("unit_price", 0)),
            gross_sales=_dec(row.get("gross_sales", 0)),
        )
        for _, row in group.iterrows()
    ]
    return order


def _sum_decimal(df: pd.DataFrame, col: str) -> Decimal:
    if col not in df.columns:
        return Decimal("0")
    return sum((_dec(v) for v in df[col].fillna(0)), Decimal("0"))


def _dec(v) -> Decimal:
    return Decimal(str(v))
