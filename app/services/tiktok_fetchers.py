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

from app.importers.tiktok_orders import HEADER_MAP, import_dataframe
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
    return result.rows_imported
