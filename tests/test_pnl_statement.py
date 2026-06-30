"""P&L statement helpers: the shared waterfall line list (used by the CSV + PDF
exports) and the index of downloadable fiscal months."""
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models.import_batch import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.order import Order, OrderType
from app.models.shop import Shop
from app.reports.monthly_pnl import MonthlyPnL
from app.reports.pnl_statement import (
    available_fiscal_months,
    statement_lines,
)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        db.add(Shop(slug="smashbox", name="Smashbox", timezone="America/Los_Angeles"))
        db.commit()
    yield


# Every required MonthlyPnL field defaults to 0 so a test can override just the
# few it cares about.
_PNL_FIELDS = [
    "gross_sales", "platform_discount", "outlandish_discount", "smashbox_discount",
    "payment_platform_discount", "refunds", "net_customer_sales", "cogs",
    "gross_profit", "tiktok_fees", "tiktok_referral_fee", "tiktok_transaction_fee",
    "tiktok_refund_admin_fee", "tiktok_sales_tax_on_referral", "tiktok_smart_promo_fee",
    "tiktok_campaign_fees", "tiktok_partner_commission", "tiktok_managed_service",
    "affiliate_commission", "shop_ads_cost", "gmv_max_ad_spend", "gmv_max_reimbursement",
    "ad_credit_offset", "shipping_revenue", "shipping_cost", "sample_shipping_cost",
    "tiktok_adjustments_net", "net_profit",
]


def _pnl(**overrides) -> MonthlyPnL:
    kw = {f: Decimal("0") for f in _PNL_FIELDS}
    kw["month"] = date(2026, 5, 1)
    kw.update({k: Decimal(str(v)) for k, v in overrides.items()})
    return MonthlyPnL(**kw)


def test_statement_lines_cover_the_waterfall():
    pnl = _pnl(gross_sales=1000, cogs=300, net_profit=250)
    lines = statement_lines(pnl)
    labels = [ln.label for ln in lines]
    # Spot-check the waterfall is present, top to bottom.
    for expected in ["Gross Product Sales", "Net Customer Sales", "Gross Profit",
                     "TikTok fees", "Shipping (to Customers)", "Net Profit"]:
        assert expected in labels


def test_statement_net_profit_uses_managed_value():
    # With zero reimbursed discounts, managed == stored. Net Profit line matches.
    pnl = _pnl(gross_sales=1000, net_profit=250)
    by_label = {ln.label: ln.amount for ln in statement_lines(pnl)}
    assert by_label["Gross Product Sales"] == Decimal("1000")
    assert by_label["Net Profit"] == pnl.managed_net_profit == Decimal("250")


def test_deductions_are_signed_negative():
    pnl = _pnl(gross_sales=1000, cogs=300, tiktok_fees=50)
    by_label = {ln.label: ln.amount for ln in statement_lines(pnl)}
    assert by_label["COGS"] == Decimal("-300")
    assert by_label["TikTok fees"] == Decimal("-50")


# ---- downloadable fiscal-month index --------------------------------------

def _paid_order(db, placed_at: datetime):
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                    original_filename="t", stored_path="t")
    db.add(b); db.flush()
    db.add(Order(import_batch_id=b.id, tiktok_order_id=f"O{placed_at.isoformat()}",
                 order_type=OrderType.PAID, status="Shipped", brand="smashbox",
                 placed_at=placed_at, gross_sales=Decimal("100")))


def test_available_fiscal_months_spans_earliest_to_current_newest_first():
    with SessionLocal() as db:
        _paid_order(db, datetime(2026, 5, 15))   # fiscal May 2026
        _paid_order(db, datetime(2026, 6, 2))    # fiscal Jun 2026
        db.commit()
        refs = available_fiscal_months(db, as_of=date(2026, 6, 28))  # current = fiscal Jun

    assert [(r.year, r.month) for r in refs] == [(2026, 6), (2026, 5)]
    assert refs[0].label == "Jun 2026"           # no "Fiscal" prefix on the page
    assert "Apr 29, 2026" in refs[1].range_str   # fiscal May = Apr 29 – May 28


def test_available_fiscal_months_empty_when_no_orders():
    with SessionLocal() as db:
        refs = available_fiscal_months(db, as_of=date(2026, 6, 28))
    assert refs == []
