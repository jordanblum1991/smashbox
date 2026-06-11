"""GMV tie-out: our computed GMV vs TikTok's stated (Shop Analytics) GMV, by
month. Variance should be ~$0 where both exist; stated is None for months the
analytics export doesn't cover; months with no activity are omitted.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.order import Order, OrderType
from app.models.tiktok_daily_metric import TikTokDailyMetric
from app.reports.reconciliation import gmv_tie_out


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _batch(db) -> ImportBatch:
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                    original_filename="f", stored_path="f")
    db.add(b); db.flush()
    return b


def _order(db, bid, oid, placed, gross):
    db.add(Order(import_batch_id=bid, tiktok_order_id=oid, placed_at=placed,
                 order_type=OrderType.PAID, status="Shipped", brand="smashbox",
                 gross_sales=Decimal(str(gross))))
    db.flush()


def _metric(db, bid, d, gmv):
    db.add(TikTokDailyMetric(import_batch_id=bid, metric_date=d, gmv=Decimal(str(gmv))))
    db.flush()


def test_gmv_tie_out_by_month():
    with SessionLocal() as db:
        b = _batch(db)
        # Mar: computed 30, stated 40 -> variance -10
        _order(db, b.id, "MAR", datetime(2026, 3, 15, 12, 0), 30)
        _metric(db, b.id, date(2026, 3, 5), 40)
        # Apr: computed 50, NO analytics -> stated None, variance None
        _order(db, b.id, "APR", datetime(2026, 4, 15, 12, 0), 50)
        # May: computed 100, stated 100 -> variance 0
        _order(db, b.id, "MAY", datetime(2026, 5, 15, 12, 0), 100)
        _metric(db, b.id, date(2026, 5, 10), 100)
        db.commit()
        rows = gmv_tie_out(db, 2026)

    months = [(r.month) for r in rows]
    assert months == [3, 4, 5]                      # Feb (no activity) omitted

    by_m = {r.month: r for r in rows}
    assert by_m[3].computed_gmv == Decimal("30.00")
    assert by_m[3].stated_gmv == Decimal("40.00")
    assert by_m[3].variance == Decimal("-10.00")

    assert by_m[4].computed_gmv == Decimal("50.00")
    assert by_m[4].stated_gmv is None
    assert by_m[4].variance is None                 # no stated -> no variance

    assert by_m[5].computed_gmv == Decimal("100.00")
    assert by_m[5].stated_gmv == Decimal("100.00")
    assert by_m[5].variance == Decimal("0.00")


def test_gmv_tie_out_empty_when_no_data():
    with SessionLocal() as db:
        _batch(db)
        rows = gmv_tie_out(db, 2026)
    assert rows == []


def test_reconciliation_page_renders_gmv_tie_out():
    """The GMV tie-out section renders on the page (exercises the Jinja
    sum/selectattr over the `variance` property + the '—' stated path)."""
    from fastapi.testclient import TestClient

    from app.main import app
    with SessionLocal() as db:
        b = _batch(db)
        _order(db, b.id, "MAY", datetime(2026, 5, 15, 12, 0), 100)
        _metric(db, b.id, date(2026, 5, 10), 100)
        _order(db, b.id, "APR", datetime(2026, 4, 15, 12, 0), 50)  # no metric → stated "—"
        db.commit()
    r = TestClient(app).get("/reports/recon-health?tab=recon&year=2026&month=5")
    assert r.status_code == 200
    assert "GMV tie-out" in r.text
