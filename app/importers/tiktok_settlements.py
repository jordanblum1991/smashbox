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
  1. Groups the Orders sheet by (Order ID, linked statement id) — the sheet
     has one row per SKU-line within an order, so multi-SKU orders appear
     multiple times. Money columns are summed across the group.
  2. Upserts one Settlement row per (order, statement) — idempotent on
     re-upload. Original per-line payloads are preserved under
     `raw_payload['lines']` for drill-down.
  3. Back-fills the matching Order row's `tiktok_fees`,
     `affiliate_commission`, `shop_ads_cost`, `shipping_cost`, and `refunds`
     so the P&L can pull from a single source (Order.*).
  4. Promotes `Sample order type == "free sample from seller"` to
     authoritative — overrides the gross_sales==0 heuristic from the
     orders-file importer.
  5. Upserts Adjustment rows by (adjustment_id, adjustment_type, create_time)
     — TikTok pairs balance/deduction rows under the same adjustment_id, so
     all three columns are needed to disambiguate the pair.

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

        # The settlement file has one row per (order, line). Aggregate up to
        # (order_id, linked_statement_id) so the natural-key uniqueness holds
        # and the money columns aren't silently overwritten line-by-line.
        orphan_ids: set[str] = set()
        for (order_id, statement_id), group in orders_df.groupby(
            [COL["order_id"], COL["linked_statement_id"]], dropna=False, sort=False
        ):
            oid = _str(order_id)
            if not oid:
                continue
            try:
                s = _upsert_settlement(db, oid, group, batch)
                if not _backfill_order(db, oid, s):
                    orphan_ids.add(oid)
                result.rows_imported += 1
            except Exception as exc:  # noqa: BLE001
                result.skip(f"settlement order {oid}: {exc}")

        if orphan_ids:
            sample = ", ".join(sorted(orphan_ids)[:3])
            more = (
                f" (+ {len(orphan_ids) - 3} more)" if len(orphan_ids) > 3 else ""
            )
            result.errors.append(
                f"orphans: {len(orphan_ids)} settlement order{'' if len(orphan_ids) == 1 else 's'} "
                f"with no matching row in the orders file — e.g. {sample}{more}. "
                f"See /reports/settlement-only-orders."
            )

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
                    _upsert_adjustment(db, aid, row, batch)
                    result.rows_imported += 1
                except Exception as exc:  # noqa: BLE001
                    result.skip(f"adjustment {aid}: {exc}")
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"(adjustments not read: {exc})")

        return result


def _settlement_payload(order_id: str, group: pd.DataFrame, batch: ImportBatch) -> dict:
    """Aggregate per-line rows of one (order, statement) into a single payload.

    Money columns are summed across the group; date / status / sample-type
    fields are taken from the first row (they don't vary within an order).
    """
    first = group.iloc[0]

    def s_pos(key: str) -> "Decimal":  # noqa: F821 — Decimal imported at top
        return sum((_pos(r, key) for _, r in group.iterrows()), Decimal("0"))

    def s_dec(col: str) -> "Decimal":
        return sum((_dec(r.get(col)) for _, r in group.iterrows()), Decimal("0"))

    fees = (
        s_pos("referral_fee")
        + s_pos("transaction_fee")
        + s_pos("refund_admin_fee")
        + s_pos("sales_tax_on_referral")
        + s_pos("smart_promo_fee")
        + s_pos("smart_promo_fee_tax")
        + s_pos("campaign_resource_fee")
        + s_pos("campaign_service_fee")
        + s_pos("tiktok_shop_partner_commission")
        + s_pos("managed_service_per_order")
        + s_pos("managed_service_sales_tax")
    )
    affiliate = (
        s_pos("affiliate_commission")
        + s_pos("affiliate_partner_commission")
        + s_pos("affiliate_commission_deposit")
        - s_pos("affiliate_commission_refund")
    )
    shop_ads = s_pos("affiliate_shop_ads_commission") + s_pos("affiliate_partner_shop_ads")
    shipping = (
        s_pos("tiktok_shipping_fee")
        + s_pos("fbt_shipping_fee")
        + s_pos("return_shipping_fee")
        + s_pos("return_shipping_label_fee")
        - s_pos("shipping_fee_subsidy")
        - s_pos("shipping_fee_discount")
    )

    return dict(
        import_batch_id=batch.id,
        tiktok_order_id=order_id,
        linked_statement_id=_str(first.get(COL["linked_statement_id"])),
        linked_payout_id=_str(first.get(COL["linked_payout_id"])),
        paid_date=_parse_ymd(first.get(COL["paid_date"])),
        settled_date=_parse_ymd(first.get(COL["settled_date"])),
        order_status=_str(first.get(COL["order_status"])),
        sample_order_type=_str(first.get(COL["sample_order_type"])),
        order_income=s_dec(COL["order_income"]),
        order_cost=s_dec(COL["order_cost"]),
        net_order_margin=s_dec(COL["net_order_margin"]),
        gross_sales=s_pos("gross_sales"),
        gross_sales_refund=s_pos("gross_sales_refund"),
        seller_discount=s_pos("seller_discount"),
        seller_discount_refund=s_pos("seller_discount_refund"),
        tiktok_fees=fees,
        affiliate_commission=affiliate,
        shop_ads_cost=shop_ads,
        shipping_cost=shipping,
        # Keep every original line so callers can drill down later.
        raw_payload={
            "lines": [
                {k: (None if pd.isna(v) else str(v)) for k, v in row.items()}
                for _, row in group.iterrows()
            ]
        },
    )


def _upsert_settlement(db: Session, order_id: str, group: pd.DataFrame, batch: ImportBatch) -> Settlement:
    """Insert or update by the natural key (tiktok_order_id, linked_statement_id)."""
    payload = _settlement_payload(order_id, group, batch)
    statement_id = payload["linked_statement_id"]

    existing = db.execute(
        select(Settlement)
        .where(Settlement.tiktok_order_id == order_id)
        .where(Settlement.linked_statement_id == statement_id)
    ).scalar_one_or_none()

    if existing is None:
        obj = Settlement(**payload)
        db.add(obj)
        return obj

    for k, v in payload.items():
        setattr(existing, k, v)
    return existing


def _upsert_adjustment(db: Session, adj_id: str, row: pd.Series, batch: ImportBatch) -> Adjustment:
    """Insert or update by (adjustment_id, adjustment_type, create_time).

    Reminder: TikTok pairs balance/deduction rows under the same adjustment_id,
    so the type column is needed to disambiguate the pair.
    """
    payload = _adjustment_payload(adj_id, row, batch)

    existing = db.execute(
        select(Adjustment)
        .where(Adjustment.adjustment_id == adj_id)
        .where(Adjustment.adjustment_type == payload["adjustment_type"])
        .where(Adjustment.create_time == payload["create_time"])
    ).scalar_one_or_none()

    if existing is None:
        obj = Adjustment(**payload)
        db.add(obj)
        return obj

    for k, v in payload.items():
        setattr(existing, k, v)
    return existing


def _adjustment_payload(adj_id: str, row: pd.Series, batch: ImportBatch) -> dict:
    return dict(
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


def _backfill_order(db: Session, order_id: str, s: Settlement) -> bool:
    """Update the matching Order with settlement-derived totals.

    Returns True if a matching Order was found and updated. False if the
    settlement references an order we don't have (caller logs these as
    'orphan' settlements so the user knows to upload a wider orders file).
    """
    order = db.execute(
        select(Order).where(Order.tiktok_order_id == order_id)
    ).scalar_one_or_none()
    if order is None:
        return False

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
    return True


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
