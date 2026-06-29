"""The P&L's GMV-Max ad spend reads the auto-synced daily metric feed
(`GmvMaxDailyMetric`), NOT the manually-uploaded TikTok Ads Manager "Cost"
export (`AdSpend`).

Why: the `AdSpend` table only refreshes when someone uploads a Cost XLSX, so it
goes stale mid-month and silently understates the P&L's "TikTok Ads (GMV Max)"
line — while the Ad Spend page (which already reads `GmvMaxDailyMetric`) shows
the live, complete number. Sourcing both from the same auto-pulled feed keeps the
two pages in lockstep and eliminates the staleness gap. See the June 2026 case:
the Cost export stopped at June 15, so the P&L read $6,361 vs the true $10,817.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.ad_spend import AdSpend
from app.models.gmv_max_daily_metric import GmvMaxDailyMetric
from app.models.order import Order, OrderLine, OrderType
from app.reports.monthly_pnl import compute_monthly_pnl


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _batch(db) -> ImportBatch:
    b = ImportBatch(kind=ImportFileKind.TIKTOK_GMV_MAX, status=ImportBatchStatus.COMPLETED,
                    original_filename="f", stored_path="f")
    db.add(b); db.flush()
    return b


def _order(db, bid, gross):
    o = Order(import_batch_id=bid, tiktok_order_id="O1", placed_at=datetime(2026, 5, 15, 12, 0),
              order_type=OrderType.PAID, status="Shipped", brand="smashbox", gross_sales=gross)
    db.add(o); db.flush()
    db.add(OrderLine(order_id=o.id, sku="SBX-1", quantity=1, unit_price=gross,
                     gross_sales=gross, unit_cogs_snapshot=Decimal("0")))
    db.flush()


def _gmvmax(db, bid, day: date, cost):
    db.add(GmvMaxDailyMetric(import_batch_id=bid, metric_date=day, cost=Decimal(cost)))
    db.flush()


def _adspend(db, bid, day: datetime, amount):
    db.add(AdSpend(import_batch_id=bid, spend_date=day, campaign_id="C1", amount=Decimal(amount)))
    db.flush()


def test_gmv_max_ad_spend_comes_from_daily_metric_feed():
    """With only the daily-metric feed populated (no Cost-export rows), the P&L's
    GMV-Max line equals the sum of GmvMaxDailyMetric.cost in the window."""
    with SessionLocal() as db:
        b = _batch(db)
        _order(db, b.id, Decimal("1000.00"))
        _gmvmax(db, b.id, date(2026, 5, 10), "120.00")
        _gmvmax(db, b.id, date(2026, 5, 20), "80.50")
        db.commit()
        p = compute_monthly_pnl(db, 2026, 5)
    assert p.gmv_max_ad_spend == Decimal("200.50")


def test_complete_daily_feed_beats_stale_cost_export():
    """The exact June-2026 bug: the manual Cost export (AdSpend) is stale and
    only covers part of the month, while the daily metric feed is complete. The
    P&L must reflect the COMPLETE daily feed, ignoring the stale Cost export."""
    with SessionLocal() as db:
        b = _batch(db)
        _order(db, b.id, Decimal("1000.00"))
        # Stale Cost export: only the first half of the month.
        _adspend(db, b.id, datetime(2026, 5, 5), "300.00")
        _adspend(db, b.id, datetime(2026, 5, 10), "300.00")
        # Complete daily feed: the whole month.
        _gmvmax(db, b.id, date(2026, 5, 5), "300.00")
        _gmvmax(db, b.id, date(2026, 5, 10), "300.00")
        _gmvmax(db, b.id, date(2026, 5, 20), "250.00")
        _gmvmax(db, b.id, date(2026, 5, 28), "150.00")
        db.commit()
        p = compute_monthly_pnl(db, 2026, 5)
    assert p.gmv_max_ad_spend == Decimal("1000.00")   # full feed, not the stale 600


def test_daily_feed_respects_window_boundaries():
    """Days outside [month-start, next-month-start) are excluded — a metric on
    the first of the next month does not bleed into this month."""
    with SessionLocal() as db:
        b = _batch(db)
        _order(db, b.id, Decimal("1000.00"))
        _gmvmax(db, b.id, date(2026, 4, 30), "11.00")   # prior month — excluded
        _gmvmax(db, b.id, date(2026, 5, 1), "22.00")    # in month
        _gmvmax(db, b.id, date(2026, 5, 31), "33.00")   # in month
        _gmvmax(db, b.id, date(2026, 6, 1), "44.00")    # next month — excluded
        db.commit()
        p = compute_monthly_pnl(db, 2026, 5)
    assert p.gmv_max_ad_spend == Decimal("55.00")       # 22 + 33 only
