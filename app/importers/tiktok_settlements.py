"""TikTok merchant_statement_profit_loss_*.xlsx importer.

The workbook has three sheets:
  - "Orders"             — one row per (statement, order) with 60 columns of
                           P&L breakdown (referral fees, affiliate commission,
                           shop ads, shipping, ...). HEADER ROW IS row 5
                           (0-indexed), real data starts at row 6.
  - "Adjustment"         — statement-level adjustments (reimbursements, bill
                           payments). HEADER ROW IS row 3.
  - "Order payment info" — per-payment breakdown. Not consumed in v1.

What this importer does:
  1. Writes one Settlement row per Orders-sheet row (raw_payload preserves
     every column verbatim).
  2. Back-fills the matching Order row's `tiktok_fees`, `affiliate_commission`,
     `shop_ads_cost`, `shipping_cost`, and `refunds` so the P&L can pull from
     a single source (Order.*).
  3. Promotes `Sample order type == "free sample from seller"` to authoritative
     — overrides the gross_sales==0 heuristic from the orders-file importer.
  4. Writes one Adjustment row per Adjustment-sheet row.

Cost conventions
----------------
TikTok reports costs as NEGATIVE numbers and inflows as POSITIVE. Our Order
model stores costs as POSITIVE magnitudes (so the P&L renderer can subtract
them directly). The `_pos` helper takes abs() to enforce that.
"""
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.importers.base import BaseImporter, ImportResult
from app.models.import_batch import ImportBatch
from app.models.order import Order, OrderType
from app.models.settlement import Adjustment, Settlement

ORDERS_HEADER_ROW = 5
ADJUSTMENT_HEADER_ROW = 3

# Column names in the Orders sheet (post header=5).
COL = {
    "order_id": "Order ID",
    "sku_id": "SKU ID",
    "sku_name": "SKU name",
    "product_name": "Product name",
    "order_income": "Order Income",
    "order_cost": "Order Cost",
    "net_order_margin": "Net Order Margin",
    "sold_qty": "Sold Quantity",
    "paid_date": "Order paid date",
    "settled_date": "Order settled date",
    "linked_statement_id": "linked statement id",
    "linked_payout_id": "linked payout id",
    "order_status": "Order status",
    "sample_order_type": "Sample order type",
    "gross_sales": "Gross sales",
    "seller_discount": "Seller discount",
    "gross_sales_refund": "Gross sales refund",
    "seller_discount_refund": "Seller discount refund",
    "affiliate_commission": "Affiliate Commission",
    "affiliate_partner_commission": "Affiliate partner commission",
    "affiliate_commission_deposit": "Affiliate commission deposit",
    "affiliate_commission_refund": "Affiliate commission refund",
    "affiliate_shop_ads_commission": "Affiliate Shop Ads commission",
    "affiliate_partner_shop_ads": "Affiliate Partner shop ads commission",
    "referral_fee": "Referral fee",
    "refund_admin_fee": "Refund administration fee",
    "transaction_fee": "Transaction fee",
    "sales_tax_on_referral": "Sales tax on referral fees",
    "smart_promo_fee": "Smart Promotion fee",
    "smart_promo_fee_tax": "Smart Promotion fee tax",
    "campaign_resource_fee": "Campaign resource fee",
    "campaign_service_fee": "Campaign service fee",
    "tiktok_shop_partner_commission": "TikTok Shop Partner commission",
    "managed_service_per_order": "Managed service plan (Per order fee)",
    "managed_service_sales_tax": "Managed service plan (Sales tax)",
    "tiktok_shipping_fee": "TikTok Shop shipping fee",
    "fbt_shipping_fee": "Fulfilled by TikTok Shop shipping fee",
    "return_shipping_fee": "Return shipping fee",
    "return_shipping_label_fee": "Return shipping label fee",
    "shipping_fee_subsidy": "Shipping fee subsidy",
    "shipping_fee_discount": "Shipping fee discount",
}

# Adjustment sheet (post header=3).
ADJ_COL = {
    "adjustment_id": "Adjustment ID",
    "adjustment_type": "Adjustment Type",
    "reason": "Adjustment reason",
    "amount": "Adjustment amount",
    "create_time": "Adjustment create time",
    "settlement_time": "Adjustment settlement time",
    "linked_statement_id": "linked statement id",
    "linked_payout_id": "llinked payout id",  # NB: typo is in TikTok's header
}


class TikTokSettlementsImporter(BaseImporter):
    def run(self, path: Path, db: Session, batch: ImportBatch) -> ImportResult:
        result = ImportResult()

        try:
            orders_df = pd.read_excel(
                path, sheet_name="Orders", header=ORDERS_HEADER_ROW, dtype=str
            )
        except ValueError as exc:
            raise ValueError(f"could not read Orders sheet: {exc}") from exc

        for _, row in orders_df.iterrows():
            order_id = _str(row.get(COL["order_id"]))
            if not order_id:
                continue
            try:
                s = _build_settlement(order_id, row, batch)
                db.add(s)
                _backfill_order(db, order_id, s)
                result.rows_imported += 1
            except Exception as exc:  # noqa: BLE001
                result.skip(f"settlement order {order_id}: {exc}")

        # Adjustments are optional — sheet may be empty.
        try:
            adj_df = pd.read_excel(
                path, sheet_name="Adjustment", header=ADJUSTMENT_HEADER_ROW, dtype=str
            )
            for _, row in adj_df.iterrows():
                aid = _str(row.get(ADJ_COL["adjustment_id"]))
                if not aid:
                    continue
                try:
                    db.add(_build_adjustment(aid, row, batch))
                    result.rows_imported += 1
                except Exception as exc:  # noqa: BLE001
                    result.skip(f"adjustment {aid}: {exc}")
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"(adjustments not read: {exc})")

        return result


def _build_settlement(order_id: str, row: pd.Series, batch: ImportBatch) -> Settlement:
    fees = (
        _pos(row, "referral_fee")
        + _pos(row, "transaction_fee")
        + _pos(row, "refund_admin_fee")
        + _pos(row, "sales_tax_on_referral")
        + _pos(row, "smart_promo_fee")
        + _pos(row, "smart_promo_fee_tax")
        + _pos(row, "campaign_resource_fee")
        + _pos(row, "campaign_service_fee")
        + _pos(row, "tiktok_shop_partner_commission")
        + _pos(row, "managed_service_per_order")
        + _pos(row, "managed_service_sales_tax")
    )
    affiliate = (
        _pos(row, "affiliate_commission")
        + _pos(row, "affiliate_partner_commission")
        + _pos(row, "affiliate_commission_deposit")
        - _pos(row, "affiliate_commission_refund")
    )
    shop_ads = (
        _pos(row, "affiliate_shop_ads_commission")
        + _pos(row, "affiliate_partner_shop_ads")
    )
    shipping = (
        _pos(row, "tiktok_shipping_fee")
        + _pos(row, "fbt_shipping_fee")
        + _pos(row, "return_shipping_fee")
        + _pos(row, "return_shipping_label_fee")
        - _pos(row, "shipping_fee_subsidy")  # subsidies offset cost
        - _pos(row, "shipping_fee_discount")
    )

    return Settlement(
        import_batch_id=batch.id,
        tiktok_order_id=order_id,
        linked_statement_id=_str(row.get(COL["linked_statement_id"])),
        linked_payout_id=_str(row.get(COL["linked_payout_id"])),
        paid_date=_parse_ymd(row.get(COL["paid_date"])),
        settled_date=_parse_ymd(row.get(COL["settled_date"])),
        order_status=_str(row.get(COL["order_status"])),
        sample_order_type=_str(row.get(COL["sample_order_type"])),
        order_income=_dec(row.get(COL["order_income"])),
        order_cost=_dec(row.get(COL["order_cost"])),
        net_order_margin=_dec(row.get(COL["net_order_margin"])),
        gross_sales=_pos(row, "gross_sales"),
        gross_sales_refund=_pos(row, "gross_sales_refund"),
        seller_discount=_pos(row, "seller_discount"),
        seller_discount_refund=_pos(row, "seller_discount_refund"),
        tiktok_fees=fees,
        affiliate_commission=affiliate,
        shop_ads_cost=shop_ads,
        shipping_cost=shipping,
        raw_payload={k: (None if pd.isna(v) else str(v)) for k, v in row.items()},
    )


def _build_adjustment(adj_id: str, row: pd.Series, batch: ImportBatch) -> Adjustment:
    return Adjustment(
        import_batch_id=batch.id,
        adjustment_id=adj_id,
        adjustment_type=_str(row.get(ADJ_COL["adjustment_type"])) or "unknown",
        reason=_str(row.get(ADJ_COL["reason"])),
        amount=_dec(row.get(ADJ_COL["amount"])),
        create_time=_parse_ymd(row.get(ADJ_COL["create_time"])),
        settlement_time=_parse_ymd(row.get(ADJ_COL["settlement_time"])),
        linked_statement_id=_str(row.get(ADJ_COL["linked_statement_id"])),
        linked_payout_id=_str(row.get(ADJ_COL["linked_payout_id"])),
    )


def _backfill_order(db: Session, order_id: str, s: Settlement) -> None:
    """Update the matching Order with settlement-derived totals."""
    order = db.execute(
        select(Order).where(Order.tiktok_order_id == order_id)
    ).scalar_one_or_none()
    if order is None:
        return  # settlement file may include orders not yet in our orders file

    # Authoritative sample flag from settlement — overrides gross_sales==0 heuristic.
    sot = (s.sample_order_type or "").strip().lower()
    if "free sample" in sot:
        order.order_type = OrderType.SAMPLE
    elif "paid sample" in sot or "oversample" in sot:
        order.order_type = OrderType.PAID_SAMPLE

    order.refunds = s.gross_sales_refund
    order.tiktok_fees = s.tiktok_fees
    order.affiliate_commission = s.affiliate_commission
    order.shop_ads_cost = s.shop_ads_cost
    order.shipping_cost = s.shipping_cost


# ---------- helpers ---------------------------------------------------------

def _str(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return None
    return s


def _dec(v) -> Decimal:
    s = _str(v)
    if s is None:
        return Decimal("0")
    try:
        return Decimal(s)
    except Exception:  # noqa: BLE001
        return Decimal("0")


def _pos(row: pd.Series, key: str) -> Decimal:
    """abs(value) for a column referenced by our internal key."""
    col = COL.get(key)
    if col is None or col not in row:
        return Decimal("0")
    return abs(_dec(row[col]))


def _parse_ymd(v) -> datetime | None:
    """TikTok dates here are ints like 20260421 ('YYYYMMDD')."""
    s = _str(v)
    if s is None:
        return None
    s = s.split(".")[0]  # drop any trailing ".0" from float coercion
    if len(s) != 8 or not s.isdigit():
        return None
    return datetime.strptime(s, "%Y%m%d")
