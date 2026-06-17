"""TikTok API responses → existing importer DataFrame seams.

Maps the live Order/Finance/Payout API into the same column shapes the file
importers expect, so the seller-funded split, sample detection, and idempotent
upsert all run through ONE code path (the importers) whether data arrives by CSV
or API. Called by `tiktok_sync._fetch_stream` once the shop is connected.

Order field-mapping (validated against prod 2026-06-15):
  - `create_time` (UTC epoch) -> America/Los_Angeles naive, matching the CSV's
    Pacific-local "Created Time" so API- and CSV-sourced orders are byte-
    compatible (placed_at, status, gross, shipping, GMV all reproduce exactly).
  - Order API status enums -> the Seller-Center display vocabulary the reports
    key off (Shipped / Completed / To ship / Canceled / Pending).
  - The 202309 API itemizes line_items per UNIT (no quantity field) -> one row
    per unit with Quantity=1; the importer's per-line split + roll-up handle it.
  - The Order API exposes only a COMBINED `platform_discount`, not the CSV's
    SKU-platform vs payment-platform split. We map it all to SKU Platform
    Discount (payment-platform = 0). GMV, net customer sales, and the seller-
    funded TOTAL stay exact; only the Outlandish/Smashbox allocation drifts on
    the rare payment-promo order — measured at $2.77 all-time across 13 of 1,545
    orders. Re-importing the orders CSV (idempotent) would correct it exactly,
    but the workflow is deliberately CSV-free.
  - Refunds are left 0 here; the settlement feed is the source of truth for
    refunds/fees and back-fills them onto the Order (matches the CSV path).
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy.orm import Session

from decimal import Decimal

from app.importers.tiktok_analytics import COL as ANALYTICS_COL, import_metric_rows
from app.importers.tiktok_orders import HEADER_MAP, import_dataframe
from app.importers.tiktok_settlements import ADJ_COL, COL, import_dataframes
from app.importers.tiktok_payouts import (
    PAY_COL,
    STMT_COL,
    import_dataframes as import_payout_dataframes,
)
from app.models.import_batch import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.tiktok_credential import TikTokCredential
from app.services import tiktok_api
from app.services.sku_resolver import resolve_all_order_lines

SHOP_TZ = ZoneInfo("America/Los_Angeles")

# Order API status enum -> Seller-Center display status the reports expect.
# Validated against stored CSV statuses on prod (AWAITING_SHIPMENT -> Pending,
# AWAITING_COLLECTION -> To ship, DELIVERED -> Shipped); the rest follow the
# same lifecycle. Unknown values pass through verbatim so a new status is
# visible in reports rather than silently mis-bucketed.
_API_STATUS_TO_DISPLAY = {
    "UNPAID": "Unpaid",
    "ON_HOLD": "On hold",
    "AWAITING_SHIPMENT": "Pending",
    "AWAITING_COLLECTION": "To ship",
    "PARTIALLY_SHIPPING": "Shipped",
    "IN_TRANSIT": "Shipped",
    "DELIVERED": "Shipped",
    "COMPLETED": "Completed",
    "CANCELLED": "Canceled",
    "CANCEL": "Canceled",
}


def _record_result(batch: ImportBatch, result) -> int:
    """Persist the importer's counts onto the batch and return rows_imported.

    The file-upload path does this in app/routers/uploads.py; the API fetchers
    must too, or the batch keeps its default `rows_imported = 0` even after
    ingesting rows (nothing else writes it) — which is why API-sync batches
    showed "0" on the freshness/uploads views despite processing orders.

    `rows_imported` counts new + updated (an upsert re-pull is mostly updates).
    The orders importer also appends an "N new, M updated" info line to
    `result.errors`, so that breakdown surfaces here as the batch's detail.
    """
    batch.rows_imported = result.rows_imported
    batch.rows_skipped = result.rows_skipped
    if result.errors:
        batch.error_message = "\n".join(result.errors[:50])
    return result.rows_imported


def display_status(api_status) -> str:
    return _API_STATUS_TO_DISPLAY.get(
        str(api_status or "").upper(), str(api_status or "unknown")
    )


def placed_at_local(create_time) -> str:
    """UTC epoch seconds -> shop-local (Pacific, DST-aware) naive timestamp
    string, matching the CSV 'Created Time' the orders importer parses."""
    dt = datetime.fromtimestamp(int(create_time), tz=timezone.utc).astimezone(SHOP_TZ)
    return dt.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def orders_to_dataframe(orders: list[dict]) -> pd.DataFrame:
    """Map Order Search API dicts into the HEADER_MAP column shape — one row per
    line-item unit. Order-level money the importer SUMS (shipping) is placed on
    the first line only; per-line money rides each line."""
    rows: list[dict] = []
    for o in orders:
        oid = str(o.get("id") or "").strip()
        status = display_status(o.get("status"))
        placed = placed_at_local(o.get("create_time"))
        pay = o.get("payment") or {}
        shipping = str(pay.get("shipping_fee") or "0")
        ship_seller_disc = str(pay.get("shipping_fee_seller_discount") or "0")
        for i, li in enumerate(o.get("line_items") or []):
            bundle_skus = ",".join(
                str(c.get("seller_sku") or "")
                for c in (li.get("combined_listing_skus") or [])
            )
            rows.append({
                HEADER_MAP["tiktok_order_id"]: oid,
                HEADER_MAP["status"]: status,
                HEADER_MAP["placed_at"]: placed,
                HEADER_MAP["sku_seller"]: str(li.get("seller_sku") or ""),
                HEADER_MAP["sku_id"]: str(li.get("sku_id") or ""),
                HEADER_MAP["bundle_skus"]: bundle_skus,
                HEADER_MAP["quantity"]: "1",  # 202309 itemizes per unit
                HEADER_MAP["unit_price"]: str(li.get("original_price") or "0"),
                HEADER_MAP["line_gross"]: str(li.get("original_price") or "0"),
                HEADER_MAP["line_seller_discount"]: str(li.get("seller_discount") or "0"),
                HEADER_MAP["line_platform_discount"]: str(li.get("platform_discount") or "0"),
                HEADER_MAP["shipping_after_discount"]: shipping if i == 0 else "0",
                HEADER_MAP["shipping_seller_discount"]: ship_seller_disc if i == 0 else "0",
                HEADER_MAP["payment_platform_discount"]: "0",  # not separable from API
                HEADER_MAP["order_refund_amount"]: "0",        # settlement back-fills
            })
    return pd.DataFrame(rows)


def fetch_orders(db: Session, cred: TikTokCredential, since: datetime | None) -> int:
    """Pull orders created at/after `since` (naive UTC watermark) from the API,
    ingest them through the orders importer seam, then resolve SKUs. Returns the
    number of orders imported. Idempotent: re-pulling overlapping orders upserts
    on tiktok_order_id and preserves settlement back-fills."""
    since_epoch = (
        int(since.replace(tzinfo=timezone.utc).timestamp()) if since is not None else None
    )
    orders = list(tiktok_api.iter_orders(cred, create_time_ge=since_epoch))
    if not orders:
        return 0

    df = orders_to_dataframe(orders)
    batch = ImportBatch(
        kind=ImportFileKind.TIKTOK_ORDERS,
        status=ImportBatchStatus.COMPLETED,
        original_filename=f"TikTok API sync · orders · {len(orders)} orders",
        stored_path="(api)",
    )
    db.add(batch)
    db.flush()
    result = import_dataframe(df, db, batch)
    resolve_all_order_lines(db)
    return _record_result(batch, result)


# --- settlements ------------------------------------------------------------
#
# Finance API field mapping, validated against 149 stored (xlsx-imported)
# settlements on prod 2026-06-15:
#   affiliate_commission = abs(affiliate_commission + affiliate_partner_commission)   [149/149]
#   shop_ads_cost        = abs(affiliate_ads_commission + partner_shop_ads)           [149/149]
#   gross_sales_refund   = abs(gross_sales_refund_amount)                             [149/149]
#   shipping_cost        = max(abs(shipping_fee_amount), abs(shipping_cost_amount))   [148/149]
#   tiktok_fees          = abs(fee_amount) - affiliate - shop_ads                     [143/149]
# The API `fee_amount` BUNDLES affiliate commission into the fee total, so we
# subtract it back out to match the importer's split. The residual fee
# (total - referral - transaction - refund_admin) is parked in the Smart
# Promotion bucket as a catch-all, since the API doesn't itemise smart-promo /
# campaign / managed-service / sales-tax-on-referral the way the xlsx does — the
# TOTAL tiktok_fees is what feeds the P&L and is reproduced. Residual fee drift
# is ~$3 all-time across ~6 orders (the sales-tax-on-referral nuance), on par
# with the orders fetcher's accepted $2.77.

def _D(x) -> Decimal:
    try:
        return Decimal(str(x if x not in (None, "") else 0))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def _ymd(epoch) -> str:
    if not epoch:
        return ""
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y%m%d")


# Adjustment (non-ORDER statement transactions) API enum -> the Seller-Center
# label the xlsx uses. Matching the label exactly keeps the natural key
# (adjustment_id, adjustment_type, create_time) idempotent with xlsx-imported
# rows. Validated against stored adjustments on prod 2026-06-15.
_ADJ_TYPE = {
    "NET_EARNINGS_BALANCE": "Net earnings balance",
    "NET_EARNINGS_DEDUCTION": "Net earnings deduction",
    "PLATFORM_REIMBURSEMENT": "TikTok Shop reimbursement",
    "LOGISTICS_REIMBURSEMENT": "Logistics reimbursement",
    "BILL_PAYMENT_(NEGATIVE_BALANCE)": "Bill payment (negative balance)",
}


def adjustment_type_label(api_type) -> str:
    t = str(api_type or "").strip()
    if t in _ADJ_TYPE:
        return _ADJ_TYPE[t]
    return t.replace("_", " ").capitalize() if t else "unknown"


def _pacific_ymd(epoch) -> str:
    """UTC epoch -> shop-local (Pacific) 'YYYYMMDD' for the settlement _parse_ymd."""
    if not epoch:
        return ""
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).astimezone(SHOP_TZ).strftime("%Y%m%d")


def settlement_transactions_to_dataframe(pairs: list[tuple[dict, dict]]) -> pd.DataFrame:
    """Map (statement, transaction) pairs into the settlement Orders-sheet COL
    shape, one row per (order, statement). Only ORDER-type transactions become
    settlement rows; non-ORDER rows are adjustments (see adjustments_to_dataframe).
    Costs are emitted as positive magnitudes; the importer's `_pos` takes abs()."""
    rows: list[dict] = []
    for stmt, t in pairs:
        if str(t.get("type") or "ORDER").upper() != "ORDER":
            continue
        referral = abs(_D(t.get("referral_fee_amount")))
        transaction = abs(_D(t.get("transaction_fee_amount")))
        refund_admin = abs(_D(t.get("refund_administration_fee_amount")))
        affiliate = abs(_D(t.get("affiliate_commission_amount"))
                        + _D(t.get("affiliate_partner_commission_amount")))
        shop_ads = abs(_D(t.get("affiliate_ads_commission_amount"))
                       + _D(t.get("affiliate_partner_shop_ads_commission_amount")))
        total_fees = abs(_D(t.get("fee_amount"))) - affiliate - shop_ads
        if total_fees < 0:
            total_fees = Decimal("0")
        residual = total_fees - referral - transaction - refund_admin
        if residual < 0:
            residual = Decimal("0")
        shipping = max(abs(_D(t.get("shipping_fee_amount"))),
                       abs(_D(t.get("shipping_cost_amount"))))
        rows.append({
            COL["order_id"]: str(t.get("order_id") or "").strip(),
            COL["linked_statement_id"]: str(stmt.get("id") or ""),
            COL["linked_payout_id"]: str(stmt.get("payment_id") or ""),
            COL["paid_date"]: _ymd(t.get("order_create_time")),
            COL["settled_date"]: _ymd(stmt.get("statement_time")),
            COL["order_income"]: str(t.get("settlement_amount") or "0"),
            COL["gross_sales"]: str(abs(_D(t.get("gross_sales_amount")))),
            COL["gross_sales_refund"]: str(abs(_D(t.get("gross_sales_refund_amount")))),
            COL["seller_discount"]: str(abs(_D(t.get("seller_discount_amount")))),
            COL["seller_discount_refund"]: str(abs(_D(t.get("seller_discount_refund_amount")))),
            COL["referral_fee"]: str(referral),
            COL["transaction_fee"]: str(transaction),
            COL["refund_admin_fee"]: str(refund_admin),
            COL["smart_promo_fee"]: str(residual),   # catch-all for un-itemised fees
            COL["affiliate_commission"]: str(affiliate),
            COL["affiliate_shop_ads_commission"]: str(shop_ads),
            COL["tiktok_shipping_fee"]: str(shipping),
        })
    return pd.DataFrame(rows)


def adjustments_to_dataframe(pairs: list[tuple[dict, dict]]) -> "pd.DataFrame | None":
    """Map the non-ORDER statement transactions (net-earnings balance/deduction,
    reimbursements, bill payments) into the Adjustment-sheet ADJ_COL shape. The
    amount keeps its sign (paired balance/deduction cancel in the P&L). Returns
    None when there are no adjustments (the importer accepts adj_df=None).

    NB: the API doesn't expose the per-adjustment `reason` text the xlsx has, so
    it's left blank — reason is display-only and nullable."""
    rows: list[dict] = []
    for stmt, t in pairs:
        if str(t.get("type") or "ORDER").upper() == "ORDER":
            continue
        aid = str(t.get("adjustment_id") or "").strip()
        if not aid:
            continue
        rows.append({
            ADJ_COL["adjustment_id"]: aid,
            ADJ_COL["adjustment_type"]: adjustment_type_label(t.get("type")),
            ADJ_COL["reason"]: "",
            ADJ_COL["amount"]: str(t.get("adjustment_amount") or "0"),
            ADJ_COL["create_time"]: _pacific_ymd(t.get("order_create_time")),
            ADJ_COL["settlement_time"]: _pacific_ymd(stmt.get("statement_time")),
            ADJ_COL["linked_statement_id"]: str(stmt.get("id") or ""),
            ADJ_COL["linked_payout_id"]: str(stmt.get("payment_id") or ""),
        })
    return pd.DataFrame(rows) if rows else None


def fetch_settlements(db: Session, cred: TikTokCredential, since: datetime | None) -> int:
    """Pull settlement statements (and their per-order transactions) since
    `since` from the Finance API, map them to the settlement importer seam, and
    back-fill Order.* fees/refunds. The same feed's non-ORDER rows become
    Adjustments (net-earnings, reimbursements, bill payments) — exactly how the
    xlsx's Orders + Adjustment sheets work. Returns rows imported. Idempotent on
    (order, statement) and (adjustment_id, type, create_time)."""
    since_epoch = (
        int(since.replace(tzinfo=timezone.utc).timestamp()) if since is not None else None
    )
    pairs = list(tiktok_api.iter_settlement_transactions(cred, statement_time_ge=since_epoch))
    if not pairs:
        return 0

    orders_df = settlement_transactions_to_dataframe(pairs)
    adj_df = adjustments_to_dataframe(pairs)
    batch = ImportBatch(
        kind=ImportFileKind.TIKTOK_SETTLEMENTS,
        status=ImportBatchStatus.COMPLETED,
        original_filename=f"TikTok API sync · settlements · {len(pairs)} txns",
        stored_path="(api)",
    )
    db.add(batch)
    db.flush()
    result = import_dataframes(orders_df, adj_df, db, batch)
    return _record_result(batch, result)


# --- payouts ----------------------------------------------------------------
#
# Validated against stored (xlsx-imported) payouts on prod 2026-06-15 — exact,
# no drift:
#   net_amount   = payment.amount.value                                   [n/n]
#   gross_amount = sum of linked statements' net_sales_amount (by payment) [n/n]
#   fees         = gross - net (computed by the importer)                  [n/n]
#   paid_at      = paid_time (Pacific date) or create_time when unpaid     [n/n]
# A payment's statements precede it by ~1-2 weeks, so we pull statements with a
# lookback margin to cover each payment's period.

_STMT_LOOKBACK_DAYS = 45


def _pacific_date(epoch) -> str:
    """UTC epoch -> shop-local (Pacific) date 'YYYY-MM-DD' for _parse_ymd.
    Empty string when the timestamp is absent (e.g. unpaid → paid_time = 0)."""
    if not epoch:
        return ""
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).astimezone(SHOP_TZ).strftime("%Y-%m-%d")


def payments_to_dataframe(payments: list[dict]) -> pd.DataFrame:
    rows = [{
        PAY_COL["payment_id"]: str(p.get("id") or "").strip(),
        PAY_COL["amount"]: str((p.get("amount") or {}).get("value") or "0"),
        PAY_COL["initiation_date"]: _pacific_date(p.get("create_time")),
        PAY_COL["completion_date"]: _pacific_date(p.get("paid_time")),
        PAY_COL["status"]: str(p.get("status") or ""),
    } for p in payments]
    return pd.DataFrame(rows)


def statements_to_payout_dataframe(statements: list[dict]) -> pd.DataFrame:
    """Statement-level rows for the payout gross/period roll-up (by Payment ID)."""
    rows = [{
        STMT_COL["statement_date"]: _pacific_date(s.get("statement_time")),
        STMT_COL["statement_id"]: str(s.get("id") or ""),
        STMT_COL["payment_id"]: str(s.get("payment_id") or ""),
        STMT_COL["net_sales"]: str(s.get("net_sales_amount") or "0"),
    } for s in statements]
    return pd.DataFrame(rows)


def fetch_payouts(db: Session, cred: TikTokCredential, since: datetime | None) -> int:
    """Pull payouts (payments) since `since` from the Finance API, plus the
    statements that roll up to their gross/period, and ingest via the payouts
    importer seam. Returns payouts imported. Idempotent on payout_id."""
    from datetime import timedelta

    since_epoch = (
        int(since.replace(tzinfo=timezone.utc).timestamp()) if since is not None else None
    )
    payments = list(tiktok_api.iter_payments(cred, create_time_ge=since_epoch))
    if not payments:
        return 0

    stmt_since = (
        int((since - timedelta(days=_STMT_LOOKBACK_DAYS)).replace(tzinfo=timezone.utc).timestamp())
        if since is not None else None
    )
    statements = list(tiktok_api.iter_statements(cred, statement_time_ge=stmt_since))

    batch = ImportBatch(
        kind=ImportFileKind.TIKTOK_PAYOUTS,
        status=ImportBatchStatus.COMPLETED,
        original_filename=f"TikTok API sync · payouts · {len(payments)} payments",
        stored_path="(api)",
    )
    db.add(batch)
    db.flush()
    result = import_payout_dataframes(
        payments_to_dataframe(payments),
        statements_to_payout_dataframe(statements),
        db, batch,
    )
    return _record_result(batch, result)


# --- analytics (daily GMV, for reconciliation) ------------------------------
#
# Maps the Shop Performance API's daily intervals onto the TikTokDailyMetric
# columns. Validated against stored (xlsx-imported) metrics on prod 2026-06-15:
# gmv and orders matched 14/14 across the sampled fortnight. Each interval is one
# day; metric_date = interval.start_date (the Seller-Center day, already Pacific).
# gmv_with_tax / tax / shipping_fees / items_refunded aren't in this endpoint, so
# they default to 0 — the reconciliation keys on gmv (+ orders), which are exact.

_ANALYTICS_LOOKBACK_DAYS = 30  # first sync (no watermark) backfills this much


def fetch_analytics(db: Session, cred: TikTokCredential, since: datetime | None) -> int:
    """Pull daily shop performance since `since` and upsert TikTokDailyMetric.
    Idempotent on metric_date; re-pulls a trailing window each run so TikTok's
    provisional recent days get corrected. Returns days imported."""
    from datetime import date, timedelta

    start = since.date() if since is not None else (date.today() - timedelta(days=_ANALYTICS_LOOKBACK_DAYS))
    end = date.today() + timedelta(days=2)  # exclusive; API caps at latest available
    intervals = tiktok_api.get_shop_performance(cred, start.isoformat(), end.isoformat())
    if not intervals:
        return 0

    rows = []
    for iv in intervals:
        sd = iv.get("start_date")
        if not sd:
            continue
        rows.append((date.fromisoformat(sd), {
            ANALYTICS_COL["gmv"]: str((iv.get("gmv") or {}).get("amount") or "0"),
            ANALYTICS_COL["orders"]: str(iv.get("orders") or 0),
            ANALYTICS_COL["customers"]: str(iv.get("buyers") or 0),
            ANALYTICS_COL["items_sold"]: str(iv.get("units_sold") or 0),
            ANALYTICS_COL["items_canceled_returned"]: str(iv.get("cancellations_and_returns") or 0),
            ANALYTICS_COL["aov"]: str((iv.get("avg_order_value") or {}).get("amount") or "0"),
        }))

    batch = ImportBatch(
        kind=ImportFileKind.TIKTOK_ANALYTICS,
        status=ImportBatchStatus.COMPLETED,
        original_filename=f"TikTok API sync · analytics · {len(rows)} days",
        stored_path="(api)",
    )
    db.add(batch)
    db.flush()
    return _record_result(batch, import_metric_rows(rows, db, batch))


# ---------------------------------------------------------------------------
# Marketing API — campaign ad spend (separate auth; see tiktok_marketing_api).
# ---------------------------------------------------------------------------
_ADS_LOOKBACK_DAYS = 30  # first sync (no watermark) backfills this much


def fetch_ad_spend(db: Session, cred, since: datetime | None) -> int:
    """Pull campaign ad spend since `since` from the TikTok Marketing API and
    upsert AdSpend. `cred` is a TikTokMarketingCredential. Idempotent on
    (spend_date, campaign_id); re-pulls a trailing window each run so TikTok's
    provisional recent days self-correct. Returns rows imported."""
    from datetime import date, timedelta

    from app.importers.tiktok_ads import import_ad_spend_rows
    from app.services import tiktok_marketing_api as mkt

    start = since.date() if since is not None else (date.today() - timedelta(days=_ADS_LOOKBACK_DAYS))
    end = date.today()  # inclusive; the report caps at the latest available day
    advertiser_ids = mkt.advertiser_id_list(cred)
    if not advertiser_ids:
        return 0

    rows: list[dict] = []
    for aid in advertiser_ids:
        for r in mkt.get_ad_spend(cred.access_token, aid, start.isoformat(), end.isoformat()):
            rows.append({
                "spend_date": datetime.fromisoformat(r["stat_day"]),  # midnight, matches CSV path
                "campaign_id": r["campaign_id"],
                "campaign_name": r.get("campaign_name"),
                "amount": r["spend"],        # canonical total the P&L subtracts
                "cash_cost": r["spend"],     # report 'spend' is real money spent
                "currency": "USD",
                "campaign_type": "AUCTION",
            })
    if not rows:
        return 0

    batch = ImportBatch(
        kind=ImportFileKind.TIKTOK_ADS,
        status=ImportBatchStatus.COMPLETED,
        original_filename=f"TikTok API sync · ad spend · {len(rows)} rows",
        stored_path="(api)",
    )
    db.add(batch)
    db.flush()
    return _record_result(batch, import_ad_spend_rows(rows, db, batch))
