"""TikTok Shop "All orders" CSV/XLSX export importer.

Header names below are taken from a real TikTok Shop export captured 2026-05-13.
If TikTok renames a column, update HEADER_MAP — do not touch the parsing loop.

Quirks of the real export:
- `Order ID` and timestamps end with a trailing tab character — stripped on read.
- Money columns mix ints, floats, and NaN — coerce everything via Decimal(str(...)).
- The CSV is one row per line item, repeated per order. Grouping by Order ID
  collapses lines back onto a single Order.
- `Seller SKU` is empty for some bundle-parent rows; we fall back to `SKU ID`
  and finally ` Virtual Bundle Seller SKU` (note the leading space).
- There is NO "free sample" flag in this file. Detection rule: order's total
  `SKU Subtotal Before Discount` == $0  →  OrderType.SAMPLE. The settlement
  file's `Sample order type` column overrides this when present.
- `SKU Seller Discount` is split between Outlandish and Smashbox; `SKU Platform
  Discount` is TikTok-funded and is NOT split (it's a TikTok-paid promo).

Seller-funded discount split (line-level, rolled up to order):
  post_tiktok_price = gross_sales − platform_discount
  outlandish        = MIN(seller_funded_discount, post_tiktok_price × 10%)
  smashbox          = seller_funded_discount − outlandish

The invariant outlandish + smashbox == seller_funded_discount holds per line
AND therefore per order (sum of equal sides). The 30% policy is checked per
line; any line breach trips the order-level discount_policy_violation flag.
"""
from decimal import Decimal
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.importers.base import BaseImporter, ImportResult
from app.models.import_batch import ImportBatch
from app.models.order import Order, OrderLine, OrderType
from app.rules.seller_funded_split import (
    split_seller_funded_discount,
    violates_policy_cap,
)

# Order columns the SETTLEMENT importer back-fills. When upserting an order
# from the orders file, these are preserved — settlement is authoritative
# for fees / refunds / commissions / shop ads / shipping cost.
_SETTLEMENT_FIELDS = (
    "refunds",
    "tiktok_fees",
    "tiktok_referral_fee",
    "tiktok_transaction_fee",
    "tiktok_refund_admin_fee",
    "tiktok_sales_tax_on_referral",
    "tiktok_smart_promo_fee",
    "tiktok_campaign_fees",
    "tiktok_partner_commission",
    "tiktok_managed_service",
    "affiliate_commission",
    "shop_ads_cost",
    "shipping_cost",
)

# Order columns the ORDERS file owns. When upserting, these are overwritten
# with the new file's values.
_ORDERS_FILE_FIELDS = (
    "placed_at",
    "status",
    "gross_sales",
    "platform_discount_total",
    "shipping_revenue",
    "seller_funded_discount_total",
    "seller_funded_outlandish",
    "seller_funded_smashbox",
    "discount_policy_violation",
)

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

        new_count = 0
        updated_count = 0

        for tiktok_order_id, group in df.groupby(HEADER_MAP["tiktok_order_id"], sort=False):
            try:
                clean_id = str(tiktok_order_id).strip().rstrip("\t").strip()
                fresh = _build_order(clean_id, group, batch)
                existing = db.execute(
                    select(Order).where(Order.tiktok_order_id == clean_id)
                ).scalar_one_or_none()
                final = _persist_upsert(db, existing, fresh, batch)
                result.rows_imported += 1
                if existing is None:
                    new_count += 1
                else:
                    updated_count += 1
                if final.discount_policy_violation:
                    for line in final.lines:
                        if not line.discount_policy_violation:
                            continue
                        pct = (
                            (line.seller_funded_discount / line.gross_sales) * 100
                            if line.gross_sales > 0 else "n/a"
                        )
                        result.errors.append(
                            f"policy: order {clean_id} sku {line.sku}: seller-funded "
                            f"{line.seller_funded_discount} on MSRP {line.gross_sales} "
                            f"({pct}%) exceeds {settings.seller_funded_policy_cap_pct * 100}% cap"
                        )
            except Exception as exc:  # noqa: BLE001
                result.skip(f"order {tiktok_order_id}: {exc}")

        if updated_count > 0:
            result.errors.append(
                f"info: {new_count} new orders inserted, {updated_count} existing orders updated "
                f"(settlement back-fills preserved)"
            )
        return result


def _persist_upsert(
    db: Session, existing: Order | None, fresh: Order, batch: ImportBatch
) -> Order:
    """Insert when no Order exists for this tiktok_order_id; otherwise update
    in place. Settlement back-filled columns and PAID_SAMPLE classification
    are preserved. Policy-violation acknowledgements are migrated by SKU."""
    if existing is None:
        db.add(fresh)
        return fresh

    # Preserve acknowledgements indexed by SKU so they survive the line replace.
    ack_skus = {ln.sku for ln in existing.lines if ln.policy_violation_acknowledged}

    # Detach fresh-built lines from the throwaway Order so we can reattach
    # them to the existing one without dual ownership.
    new_lines = list(fresh.lines)
    fresh.lines = []

    # Delete old lines (delete-orphan cascade) and flush before reattaching.
    existing.lines.clear()
    db.flush()

    # Update orders-file-owned columns; preserve settlement-owned columns.
    for f in _ORDERS_FILE_FIELDS:
        setattr(existing, f, getattr(fresh, f))
    # Settlement may have promoted SAMPLE → PAID_SAMPLE — don't unwind that.
    if existing.order_type != OrderType.PAID_SAMPLE:
        existing.order_type = fresh.order_type
    existing.import_batch_id = batch.id  # latest batch owns this row going forward

    # Attach new lines (with acknowledgement preserved for matching SKUs).
    for ln in new_lines:
        if ln.sku in ack_skus and ln.discount_policy_violation:
            ln.policy_violation_acknowledged = True
        existing.lines.append(ln)

    return existing


def _read_any(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(path, dtype=str)
    if suffix == ".csv":
        return pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])
    raise ValueError(f"unsupported file type: {suffix}")


def _validate_headers(df: pd.DataFrame) -> pd.DataFrame:
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

    # Build lines first — the per-line split is the source of truth.
    lines = [_build_line(row) for _, row in group.iterrows()]

    # Roll up to order totals.
    order_gross = sum((ln.gross_sales for ln in lines), Decimal("0"))
    order_platform_disc = sum((ln.platform_discount for ln in lines), Decimal("0"))
    order_sf_total = sum((ln.seller_funded_discount for ln in lines), Decimal("0"))
    order_outlandish = sum((ln.seller_funded_outlandish for ln in lines), Decimal("0"))
    order_smashbox = sum((ln.seller_funded_smashbox for ln in lines), Decimal("0"))
    any_violation = any(ln.discount_policy_violation for ln in lines)

    # Sanity belt: the per-line sum invariant rolls up exactly.
    assert order_outlandish + order_smashbox == order_sf_total, (
        f"order {tiktok_order_id}: roll-up drift "
        f"{order_outlandish} + {order_smashbox} != {order_sf_total}"
    )

    order_type = OrderType.SAMPLE if order_gross == Decimal("0") else OrderType.PAID

    order = Order(
        import_batch_id=batch.id,
        tiktok_order_id=tiktok_order_id,
        placed_at=_parse_ts(first[HEADER_MAP["placed_at"]]),
        order_type=order_type,
        status=str(first.get(HEADER_MAP["status"], "unknown") or "unknown"),
        brand=settings.default_brand,
        gross_sales=order_gross,
        platform_discount_total=order_platform_disc,
        refunds=_max_decimal(group, HEADER_MAP["order_refund_amount"]),
        shipping_revenue=_sum_decimal(group, HEADER_MAP["shipping_after_discount"]),
        seller_funded_discount_total=order_sf_total,
        seller_funded_outlandish=order_outlandish,
        seller_funded_smashbox=order_smashbox,
        discount_policy_violation=any_violation,
    )
    order.lines = lines
    return order


def _build_line(row: pd.Series) -> OrderLine:
    gross = _to_decimal(row.get(HEADER_MAP["line_gross"], 0))
    platform_disc = _to_decimal(row.get(HEADER_MAP["line_platform_discount"], 0))
    seller_disc = _to_decimal(row.get(HEADER_MAP["line_seller_discount"], 0))

    # Post-TikTok price is the eligible base for the seller-funded split.
    post_tiktok = (gross - platform_disc).quantize(Decimal("0.01"))
    if post_tiktok < Decimal("0"):
        post_tiktok = Decimal("0.00")

    split = split_seller_funded_discount(seller_disc, eligible_base=post_tiktok)
    # Policy ceiling uses MSRP (gross), not post-TikTok — conventional
    # discount-percentage language: "no SKU goes over 30% off retail."
    violation = violates_policy_cap(seller_disc, eligible_base=gross)

    return OrderLine(
        sku=_resolve_sku(row),
        quantity=int(_to_decimal(row.get(HEADER_MAP["quantity"], 1))),
        unit_price=_to_decimal(row.get(HEADER_MAP["unit_price"], 0)),
        gross_sales=gross,
        platform_discount=platform_disc,
        post_tiktok_price=post_tiktok,
        seller_funded_discount=split.total,
        seller_funded_outlandish=split.outlandish,
        seller_funded_smashbox=split.smashbox,
        discount_policy_violation=violation,
    )


def _resolve_sku(row: pd.Series) -> str:
    """Return the strongest identifier available.

    Prefer the TikTok SKU ID (numeric, always present in exports) — that is
    the canonical product key the catalog tables (Sku.tiktok_sku_id,
    Bundle.tiktok_sku_id) use. Fall back to Seller SKU then bundle SKUs only
    when the SKU ID is genuinely missing.
    """
    for key in ("sku_id", "sku_seller", "bundle_skus"):
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
    return pd.to_datetime(cleaned, errors="raise").to_pydatetime()


def _sum_decimal(df: pd.DataFrame, col: str) -> Decimal:
    if col not in df.columns:
        return Decimal("0")
    return sum((_to_decimal(v) for v in df[col]), Decimal("0"))


def _max_decimal(df: pd.DataFrame, col: str) -> Decimal:
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
