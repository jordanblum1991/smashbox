"""TikTok Shop "All orders" CSV/XLSX export importer.

Header names below are taken from a real TikTok Shop export captured 2026-05-13.
If TikTok renames a column, update HEADER_MAP — do not touch the parsing loop.

Quirks of the real export:
- Timestamps end with a trailing tab character (`05/12/2026 10:26:17 PM\t`).
- Money columns mix ints, floats, and NaN — coerce everything via Decimal(str(...)).
- The CSV is one row per line item, repeated per order. Grouping by Order ID
  collapses lines back onto a single Order.
- `Seller SKU` is empty for some bundle-parent rows; we fall back to `SKU ID`
  and finally ` Virtual Bundle Seller SKU` (note the leading space).
- There is NO "free sample" flag in this file. Sample detection rule:
  if the order's total `SKU Subtotal Before Discount` is $0, it's a SAMPLE.
  This is the business rule confirmed 2026-05-13.
- `SKU Seller Discount` is the seller-funded portion we split between
  Outlandish and Smashbox. `SKU Platform Discount` is TikTok's promo and is
  NOT seller-funded.
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

# "our internal name" -> "real TikTok header"
HEADER_MAP = {
    "tiktok_order_id": "Order ID",
    "status": "Order Status",
    "placed_at": "Created Time",
    "sku_seller": "Seller SKU",
    "sku_id": "SKU ID",
    "bundle_skus": " Virtual Bundle Seller SKU",  # NB: leading space
    "quantity": "Quantity",
    "unit_price": "SKU Unit Original Price",
    "line_gross": "SKU Subtotal Before Discount",
    "line_seller_discount": "SKU Seller Discount",       # we split THIS
    "line_platform_discount": "SKU Platform Discount",    # TikTok-funded, not split
    "shipping_after_discount": "Shipping Fee After Discount",
    "shipping_seller_discount": "Shipping Fee Seller Discount",
    "order_refund_amount": "Order Refund Amount",
}


class TikTokOrdersImporter(BaseImporter):
    def run(self, path: Path, db: Session, batch: ImportBatch) -> ImportResult:
        result = ImportResult()
        df = _read_any(path)
        df = _validate_headers(df)

        for tiktok_order_id, group in df.groupby(HEADER_MAP["tiktok_order_id"], sort=False):
            try:
                order = _build_order(str(tiktok_order_id), group, batch)
                db.add(order)
                result.rows_imported += 1
            except Exception as exc:  # noqa: BLE001
                result.skip(f"order {tiktok_order_id}: {exc}")

        return result


def _read_any(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(path, dtype=str)
    if suffix == ".csv":
        # dtype=str keeps everything as-is so we can parse with Decimal later.
        return pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])
    raise ValueError(f"unsupported file type: {suffix}")


def _validate_headers(df: pd.DataFrame) -> pd.DataFrame:
    """Check the required headers are present. Soft-fail on optional ones."""
    required = [
        HEADER_MAP["tiktok_order_id"],
        HEADER_MAP["placed_at"],
        HEADER_MAP["quantity"],
        HEADER_MAP["line_gross"],
    ]
    missing = [h for h in required if h not in df.columns]
    if missing:
        raise ValueError(f"missing required TikTok columns: {missing}")
    return df


def _build_order(tiktok_order_id: str, group: pd.DataFrame, batch: ImportBatch) -> Order:
    first = group.iloc[0]

    line_gross = _sum_decimal(group, HEADER_MAP["line_gross"])
    line_seller_disc = _sum_decimal(group, HEADER_MAP["line_seller_discount"])
    shipping_after = _sum_decimal(group, HEADER_MAP["shipping_after_discount"])
    order_refund = _max_decimal(group, HEADER_MAP["order_refund_amount"])

    split = split_seller_funded_discount(line_seller_disc)
    order_type = OrderType.SAMPLE if line_gross == Decimal("0") else OrderType.PAID

    order = Order(
        import_batch_id=batch.id,
        tiktok_order_id=tiktok_order_id,
        placed_at=_parse_ts(first[HEADER_MAP["placed_at"]]),
        order_type=order_type,
        status=str(first.get(HEADER_MAP["status"], "unknown") or "unknown"),
        brand=settings.default_brand,
        gross_sales=line_gross,
        refunds=order_refund,
        shipping_revenue=shipping_after,
        seller_funded_discount_total=split.total,
        seller_funded_outlandish=split.outlandish,
        seller_funded_smashbox=split.smashbox,
    )
    order.lines = [_build_line(row) for _, row in group.iterrows()]
    return order


def _build_line(row: pd.Series) -> OrderLine:
    return OrderLine(
        sku=_resolve_sku(row),
        quantity=int(_to_decimal(row.get(HEADER_MAP["quantity"], 1))),
        unit_price=_to_decimal(row.get(HEADER_MAP["unit_price"], 0)),
        gross_sales=_to_decimal(row.get(HEADER_MAP["line_gross"], 0)),
    )


def _resolve_sku(row: pd.Series) -> str:
    """Prefer Seller SKU; fall back to SKU ID; then to bundle SKUs joined."""
    for key in ("sku_seller", "sku_id", "bundle_skus"):
        col = HEADER_MAP[key]
        if col not in row:
            continue
        val = row[col]
        if pd.isna(val):
            continue
        s = str(val).strip()
        if s and s.lower() != "nan":
            return s
    return "UNKNOWN"


def _parse_ts(value) -> pd.Timestamp:
    """TikTok timestamps end with `\\t`. Strip and parse."""
    if pd.isna(value):
        raise ValueError("missing Created Time")
    cleaned = str(value).strip().rstrip("\t").strip()
    ts = pd.to_datetime(cleaned, errors="raise")
    return ts.to_pydatetime()


def _sum_decimal(df: pd.DataFrame, col: str) -> Decimal:
    if col not in df.columns:
        return Decimal("0")
    return sum((_to_decimal(v) for v in df[col]), Decimal("0"))


def _max_decimal(df: pd.DataFrame, col: str) -> Decimal:
    """For order-level fields that repeat or appear only once per order."""
    if col not in df.columns:
        return Decimal("0")
    values = [_to_decimal(v) for v in df[col]]
    return max(values) if values else Decimal("0")


def _to_decimal(v) -> Decimal:
    if v is None:
        return Decimal("0")
    if isinstance(v, float) and pd.isna(v):
        return Decimal("0")
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return Decimal("0")
    return Decimal(s)
