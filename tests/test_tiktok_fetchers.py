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
from app.models.settlement import Settlement
from app.models.payout import Payout
from app.services.tiktok_fetchers import (
    display_status,
    fetch_payouts,
    fetch_settlements,
    orders_to_dataframe,
    payments_to_dataframe,
    placed_at_local,
    settlement_transactions_to_dataframe,
    statements_to_payout_dataframe,
)
from app.importers.tiktok_orders import import_dataframe
from app.importers.tiktok_settlements import import_dataframes
from app.importers.tiktok_payouts import import_dataframes as import_payout_dataframes


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


# --- settlements ------------------------------------------------------------

def _stmt_txn(**over):
    """A (statement, transaction) pair mirroring the real Finance API shape —
    the cracked example: fee_amount bundles affiliate, so tiktok_fees=3.50."""
    stmt = {"id": "STMT1", "statement_time": 1781395200, "payment_id": "PAY1"}
    txn = {
        "order_id": "577428622655590476",
        "order_create_time": 1781309717,
        "fee_amount": "-7.18",                       # bundles affiliate
        "referral_fee_amount": "-2.21",
        "transaction_fee_amount": "0",
        "refund_administration_fee_amount": "0",
        "affiliate_commission_amount": "-3.68",
        "affiliate_partner_commission_amount": "0",
        "affiliate_ads_commission_amount": "0",
        "shipping_fee_amount": "-4.31",
        "shipping_cost_amount": "0",
        "gross_sales_amount": "35",
        "gross_sales_refund_amount": "0",
        "seller_discount_amount": "-7",
        "seller_discount_refund_amount": "0",
    }
    txn.update(over)
    return [(stmt, txn)]


def _ingest_settlements(pairs):
    with SessionLocal() as db:
        batch = ImportBatch(
            kind=ImportFileKind.TIKTOK_SETTLEMENTS,
            status=ImportBatchStatus.COMPLETED,
            original_filename="api", stored_path="(api)",
        )
        db.add(batch)
        db.flush()
        import_dataframes(settlement_transactions_to_dataframe(pairs), None, db, batch)
        db.commit()
    with SessionLocal() as db:
        return db.query(Settlement).all()


def test_settlement_fee_total_subtracts_bundled_affiliate():
    """API fee_amount bundles affiliate; the fetcher subtracts it so tiktok_fees
    matches the importer's affiliate-excluded definition (validated: 3.50)."""
    s = _ingest_settlements(_stmt_txn())[0]
    assert s.tiktok_fees == Decimal("3.50")            # abs(7.18) - 3.68
    assert s.affiliate_commission == Decimal("3.68")
    assert s.shop_ads_cost == Decimal("0.00")
    assert s.shipping_cost == Decimal("4.31")          # max(4.31, 0)
    assert s.tiktok_referral_fee == Decimal("2.21")
    # Residual (total - referral - txn - refund_admin) parked in smart-promo.
    assert s.tiktok_smart_promo_fee == Decimal("1.29")
    # The 8 buckets must sum to tiktok_fees by construction.
    assert s.tiktok_referral_fee + s.tiktok_smart_promo_fee == s.tiktok_fees
    assert s.linked_statement_id == "STMT1"


def test_settlement_shipping_uses_max_of_the_two_shipping_fields():
    """FBT orders report shipping in shipping_cost_amount, FBM in
    shipping_fee_amount — max() captures whichever the order used."""
    s = _ingest_settlements(_stmt_txn(shipping_fee_amount="0", shipping_cost_amount="-5.00"))[0]
    assert s.shipping_cost == Decimal("5.00")


def test_fetch_settlements_returns_zero_when_no_statements(monkeypatch):
    from app.services import tiktok_api
    monkeypatch.setattr(tiktok_api, "iter_settlement_transactions", lambda *a, **k: iter(()))
    with SessionLocal() as db:
        assert fetch_settlements(db, object(), None) == 0


# --- payouts ----------------------------------------------------------------

def test_payout_net_gross_fees_and_date_reproduce_the_xlsx():
    """Cash side from /payments, gross from summing the linked statements'
    net_sales — validated exact on prod: net 2454.78, gross 3973.60, fees 1518.82."""
    payments = [{
        "id": "PAY1",
        "amount": {"currency": "USD", "value": "2454.78"},
        "create_time": 1781503456,   # -> 2026-06-14 Pacific
        "paid_time": 0,              # unpaid -> falls back to initiation date
        "status": "PROCESSING",
    }]
    statements = [
        {"id": "S1", "payment_id": "PAY1", "statement_time": 1781200000, "net_sales_amount": "2000.00"},
        {"id": "S2", "payment_id": "PAY1", "statement_time": 1781300000, "net_sales_amount": "1973.60"},
        {"id": "S3", "payment_id": "OTHER", "statement_time": 1781300000, "net_sales_amount": "999.99"},
    ]
    with SessionLocal() as db:
        batch = ImportBatch(kind=ImportFileKind.TIKTOK_PAYOUTS,
                            status=ImportBatchStatus.COMPLETED,
                            original_filename="api", stored_path="(api)")
        db.add(batch); db.flush()
        import_payout_dataframes(
            payments_to_dataframe(payments),
            statements_to_payout_dataframe(statements),
            db, batch,
        )
        db.commit()
        p = db.query(Payout).one()   # only PAY1 — OTHER has no payment row
    assert p.payout_id == "PAY1"
    assert p.net_amount == Decimal("2454.78")
    assert p.gross_amount == Decimal("3973.60")   # 2000 + 1973.60 (OTHER excluded)
    assert p.fees == Decimal("1518.82")           # gross - net
    assert p.paid_at == datetime(2026, 6, 14)     # create_time, Pacific date


def test_fetch_payouts_returns_zero_when_no_payments(monkeypatch):
    from app.services import tiktok_api
    monkeypatch.setattr(tiktok_api, "iter_payments", lambda *a, **k: iter(()))
    with SessionLocal() as db:
        assert fetch_payouts(db, object(), None) == 0
