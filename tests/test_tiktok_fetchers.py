"""TikTok API → importer mapping (orders fetcher).

Locks in the field mapping validated against prod on 2026-06-15: the Order
Search JSON, run through `orders_to_dataframe` + the orders importer seam,
reproduces the CSV import's placed_at (Pacific-local), status vocabulary,
gross, shipping, and GMV. The combined-platform-discount limitation (API can't
split SKU- vs payment-platform) is asserted explicitly so a future change is a
conscious one.
"""
from datetime import datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import (
    ImportBatch,
    ImportBatchStatus,
    ImportFileKind,
    Order,
    OrderType,
)
from app.services.tiktok_fetchers import (
    display_status,
    orders_to_dataframe,
    placed_at_local,
)
from app.importers.tiktok_orders import import_dataframe


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


# --- pure mappers -----------------------------------------------------------

def test_status_maps_api_enum_to_seller_center_vocabulary():
    assert display_status("AWAITING_COLLECTION") == "To ship"
    assert display_status("AWAITING_SHIPMENT") == "Pending"
    assert display_status("DELIVERED") == "Shipped"
    assert display_status("COMPLETED") == "Completed"
    assert display_status("CANCELLED") == "Canceled"
    # Unknown statuses pass through verbatim (visible, not silently mis-bucketed)
    assert display_status("SOME_NEW_STATUS") == "SOME_NEW_STATUS"


def test_create_time_epoch_converts_to_pacific_local_naive():
    # 1781382542 == 2026-06-13 20:29:02 UTC == 13:29:02 PDT (UTC-7).
    assert placed_at_local(1781382542) == "2026-06-13 13:29:02"
    # Winter lands in PST (UTC-8), proving DST-awareness:
    # 1768435200 == 2026-01-15 00:00:00 UTC -> 2026-01-14 16:00:00 PST.
    assert placed_at_local(1768435200) == "2026-01-14 16:00:00"


# --- full round-trip through the importer seam ------------------------------

def _api_order(**over):
    """A single-line Order Search payload mirroring the real 202309 shape."""
    o = {
        "id": "577432269090755281",
        "status": "AWAITING_COLLECTION",
        "create_time": 1781382542,
        "payment": {"shipping_fee": "0", "shipping_fee_seller_discount": "3.57"},
        "line_items": [{
            "seller_sku": "SBX-C01101",
            "sku_id": "1729492097758368939",
            "original_price": "35",
            "seller_discount": "7",
            "platform_discount": "5.2",
            "combined_listing_skus": [],
        }],
    }
    o.update(over)
    return o


def _ingest(orders):
    with SessionLocal() as db:
        batch = ImportBatch(
            kind=ImportFileKind.TIKTOK_ORDERS,
            status=ImportBatchStatus.COMPLETED,
            original_filename="api", stored_path="(api)",
        )
        db.add(batch)
        db.flush()
        import_dataframe(orders_to_dataframe(orders), db, batch)
        db.commit()
    with SessionLocal() as db:
        return db.query(Order).all()


def test_order_roundtrips_through_importer_with_exact_headline_figures():
    orders = _ingest([_api_order()])
    assert len(orders) == 1
    o = orders[0]
    assert o.tiktok_order_id == "577432269090755281"
    assert o.placed_at == datetime(2026, 6, 13, 13, 29, 2)
    assert o.status == "To ship"
    assert o.order_type == OrderType.PAID
    assert o.gross_sales == Decimal("35.00")
    assert o.shipping_revenue == Decimal("0.00")
    assert o.seller_funded_discount_total == Decimal("7.00")
    # The exact-sum invariant always holds, regardless of the platform-split gap.
    assert o.seller_funded_outlandish + o.seller_funded_smashbox == o.seller_funded_discount_total


def test_combined_platform_discount_is_mapped_to_sku_platform():
    """Documents the known limitation: the API's single platform_discount becomes
    SKU Platform Discount, with payment-platform = 0. GMV stays correct; only the
    Outlandish/Smashbox split base differs from a CSV that separated the two."""
    o = _ingest([_api_order()])[0]
    assert o.platform_discount_total == Decimal("5.20")
    assert o.payment_platform_discount == Decimal("0.00")
    # GMV = gross + shipping - seller promos - platform - payment-platform
    gmv = (o.gross_sales + o.shipping_revenue
           - o.seller_funded_outlandish - o.seller_funded_smashbox
           - o.platform_discount_total - o.payment_platform_discount)
    assert gmv == Decimal("22.80")


def test_zero_gross_order_is_classified_as_sample():
    sample = _api_order(line_items=[{
        "seller_sku": "SBX-FREE", "sku_id": "999", "original_price": "0",
        "seller_discount": "0", "platform_discount": "0", "combined_listing_skus": [],
    }])
    o = _ingest([sample])[0]
    assert o.order_type == OrderType.SAMPLE


def test_per_unit_line_items_roll_up_to_quantity():
    """202309 itemizes per unit; two line_items for the same SKU become two
    qty-1 lines that roll up to a 2-unit order."""
    two_units = _api_order(line_items=[
        {"seller_sku": "SBX-A", "sku_id": "111", "original_price": "10",
         "seller_discount": "0", "platform_discount": "0", "combined_listing_skus": []},
        {"seller_sku": "SBX-A", "sku_id": "111", "original_price": "10",
         "seller_discount": "0", "platform_discount": "0", "combined_listing_skus": []},
    ])
    with SessionLocal() as db:
        batch = ImportBatch(
            kind=ImportFileKind.TIKTOK_ORDERS,
            status=ImportBatchStatus.COMPLETED,
            original_filename="api", stored_path="(api)",
        )
        db.add(batch)
        db.flush()
        import_dataframe(orders_to_dataframe([two_units]), db, batch)
        db.commit()
        order = db.query(Order).one()
        assert order.gross_sales == Decimal("20.00")
        assert sum(ln.quantity for ln in order.lines) == 2
