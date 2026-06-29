"""Contra net-zero presentation of the TikTok-Funded (platform) discount.

TikTok funds the platform discount and reimburses it: settlements credit
merchant revenue on the full gross basis (Gross − Seller discount), never
deducting the platform discount. Verified against settlement Net Order Margin
for fiscal Mar/Apr 2026 to the dollar. The stored P&L fields stay
settlement/GMV-real (platform still subtracted) so the daily GMV reconciliation
keeps tying to Seller Center; the `managed_*` properties add the platform offset
back (mirroring the Smashbox-funded offset) so the rendered P&L reflects the
reimbursement we actually receive.

Guards:
  - the auto-derived platform offset always equals the platform discount;
  - managed_net_profit is unchanged whether platform discount is $0 or positive;
  - stored net_customer_sales still subtracts platform (recon tie-out preserved);
  - managed_* adds BOTH the platform and smashbox offsets back.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import (
    ImportBatch,
    ImportBatchStatus,
    ImportFileKind,
    Order,
    OrderLine,
    OrderType,
)
from app.reports.monthly_pnl import compute_monthly_pnl
from app.reports.pnl import PeriodKind, compute_pnl_view


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _batch(db) -> ImportBatch:
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                    original_filename="t.csv", stored_path="/tmp/t.csv")
    db.add(b); db.flush()
    return b


def _paid_order(db, batch_id, placed_at, *, tt_id, gross=Decimal("100.00"),
                platform=Decimal("0"), outlandish=Decimal("0"), smashbox=Decimal("0"),
                refunds=Decimal("0"), tiktok_fees=Decimal("0")) -> Order:
    o = Order(import_batch_id=batch_id, tiktok_order_id=tt_id, placed_at=placed_at,
              order_type=OrderType.PAID, status="Shipped", brand="smashbox",
              gross_sales=gross, platform_discount_total=platform,
              seller_funded_outlandish=outlandish, seller_funded_smashbox=smashbox,
              refunds=refunds, tiktok_fees=tiktok_fees, tiktok_referral_fee=tiktok_fees)
    db.add(o); db.flush()
    db.add(OrderLine(order_id=o.id, sku="SBX-001", quantity=1, unit_price=gross,
                     gross_sales=gross, unit_cogs_snapshot=Decimal("0")))
    db.flush()
    return o


@pytest.mark.parametrize("period_kind", ["MONTH", "YTD", "YEAR", "CUSTOM", "RANGE"])
def test_platform_discount_offset_equals_discount(period_kind):
    with SessionLocal() as db:
        b = _batch(db)
        _paid_order(db, b.id, datetime(2026, 5, 12, 10), tt_id="T1",
                    gross=Decimal("500.00"), platform=Decimal("40.00"))
        _paid_order(db, b.id, datetime(2026, 5, 20, 14), tt_id="T2",
                    gross=Decimal("200.00"), platform=Decimal("15.00"))
        db.commit()
    with SessionLocal() as db:
        if period_kind == "MONTH":
            v = compute_pnl_view(db, PeriodKind.MONTH, year=2026, month=5)
        elif period_kind == "YTD":
            v = compute_pnl_view(db, PeriodKind.YTD, year=2026, month=5)
        elif period_kind == "YEAR":
            v = compute_pnl_view(db, PeriodKind.YEAR, year=2026)
        elif period_kind == "CUSTOM":
            v = compute_pnl_view(db, PeriodKind.CUSTOM,
                                 start_date=date(2026, 5, 1), end_date=date(2026, 5, 31))
        else:
            v = compute_pnl_view(db, PeriodKind.RANGE,
                                 start_year=2026, start_month=4, end_year=2026, end_month=6)
    expected = Decimal("55.00")  # 40 + 15
    assert v.total.platform_discount == expected
    assert v.total.platform_discount_offset == expected, period_kind
    # The deduction + the contra add-back net to $0 in every period kind.
    assert v.total.platform_discount + (-v.total.platform_discount_offset) == Decimal("0")


def test_managed_net_profit_invariant_when_platform_zero():
    """Net profit on the rendered P&L is identical whether TikTok funded $0 or
    a positive platform discount — because TikTok reimburses it."""
    with SessionLocal() as db:
        b = _batch(db)
        _paid_order(db, b.id, datetime(2026, 5, 12), tt_id="WITH",
                    gross=Decimal("1000.00"), platform=Decimal("120.00"),
                    tiktok_fees=Decimal("50.00"))
        db.commit()
    with SessionLocal() as db:
        v_with = compute_monthly_pnl(db, 2026, 5)

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    with SessionLocal() as db:
        b = _batch(db)
        _paid_order(db, b.id, datetime(2026, 5, 12), tt_id="WITHOUT",
                    gross=Decimal("1000.00"), platform=Decimal("0.00"),
                    tiktok_fees=Decimal("50.00"))
        db.commit()
    with SessionLocal() as db:
        v_without = compute_monthly_pnl(db, 2026, 5)

    assert v_with.managed_net_profit == v_without.managed_net_profit
    # The settlement-real net_profit (stored) DOES differ by the platform amount.
    assert v_with.net_profit + Decimal("120.00") == v_without.net_profit


def test_stored_fields_unchanged_managed_adds_both_offsets():
    """Stored net_customer_sales stays settlement/GMV-real (still subtracts
    platform). managed_* adds BOTH the platform and smashbox offsets back."""
    with SessionLocal() as db:
        b = _batch(db)
        _paid_order(db, b.id, datetime(2026, 5, 12), tt_id="T1",
                    gross=Decimal("1000.00"), platform=Decimal("100.00"),
                    outlandish=Decimal("50.00"), smashbox=Decimal("75.00"),
                    refunds=Decimal("20.00"))
        db.commit()
    with SessionLocal() as db:
        v = compute_monthly_pnl(db, 2026, 5)
    # Stored: gross - platform - outlandish - smashbox - refunds
    assert v.net_customer_sales == Decimal("755.00")
    assert v.platform_discount_offset == Decimal("100.00")
    # managed adds BOTH offsets: 755 + 75 (smashbox) + 100 (platform)
    assert v.managed_net_customer_sales == Decimal("930.00")
    # managed_net_profit adds both offsets to stored net_profit
    assert v.managed_net_profit == v.net_profit + Decimal("75.00") + Decimal("100.00")
