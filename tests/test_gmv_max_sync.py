"""GMV-Max API sync: 30-day chunker, by-day aggregation/parity, idempotency,
and the no-credential / no-campaign paths. The Marketing-API seams are stubbed
so no network is touched."""
from datetime import date
from decimal import Decimal

import pytest

import app.services.gmv_max_sync as sync_mod
from app.db import Base, SessionLocal, engine
from app.models.gmv_max_daily_metric import GmvMaxDailyMetric
from app.models.import_batch import ImportBatchStatus
from app.models.tiktok_marketing_credential import TikTokMarketingCredential


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def test_date_chunks_splits_into_30_day_windows():
    chunks = sync_mod._date_chunks(date(2026, 5, 1), date(2026, 6, 4), max_days=30)
    assert chunks == [
        (date(2026, 5, 1), date(2026, 5, 30)),
        (date(2026, 5, 31), date(2026, 6, 4)),
    ]


def test_date_chunks_single_window():
    assert sync_mod._date_chunks(date(2026, 5, 1), date(2026, 5, 10), max_days=30) == [
        (date(2026, 5, 1), date(2026, 5, 10)),
    ]


def _connect(db):
    db.add(TikTokMarketingCredential(access_token="tok", advertiser_id="adv1",
                                     advertiser_ids="adv1"))
    db.commit()


def _stub_api(monkeypatch, *, campaigns, stores, report_rows):
    monkeypatch.setattr(sync_mod.mapi, "list_gmv_max_campaigns",
                        lambda tok, adv: list(campaigns))
    monkeypatch.setattr(sync_mod.mapi, "gmv_max_store_ids",
                        lambda tok, adv, camps: list(stores))
    monkeypatch.setattr(sync_mod.mapi, "get_gmv_max_report",
                        lambda tok, adv, st, s, e: list(report_rows))


def test_sync_aggregates_by_day_and_writes(monkeypatch):
    with SessionLocal() as db:
        _connect(db)
        _stub_api(monkeypatch,
                  campaigns=[{"campaign_id": "1"}],
                  stores=["STORE_A"],
                  report_rows=[
                      {"stat_day": "2026-05-10", "cost": Decimal("60.00"),
                       "orders": 3, "gross_revenue": Decimal("180.00")},
                      {"stat_day": "2026-05-10", "cost": Decimal("40.00"),
                       "orders": 2, "gross_revenue": Decimal("120.00")},
                  ])
        batch = sync_mod.sync_gmv_max(db, lookback_days=35, today=date(2026, 5, 12))
        assert batch.status == ImportBatchStatus.COMPLETED
        rows = db.query(GmvMaxDailyMetric).all()
        assert len(rows) == 1
        assert rows[0].metric_date == date(2026, 5, 10)
        assert rows[0].cost == Decimal("100.00")
        assert rows[0].sku_orders == 5
        assert rows[0].gross_revenue == Decimal("300.00")


def test_sync_is_idempotent(monkeypatch):
    rows = [{"stat_day": "2026-05-10", "cost": Decimal("100.00"),
             "orders": 5, "gross_revenue": Decimal("300.00")}]
    with SessionLocal() as db:
        _connect(db)
        _stub_api(monkeypatch, campaigns=[{"campaign_id": "1"}],
                  stores=["STORE_A"], report_rows=rows)
        sync_mod.sync_gmv_max(db, today=date(2026, 5, 12))
        sync_mod.sync_gmv_max(db, today=date(2026, 5, 12))
        all_rows = db.query(GmvMaxDailyMetric).all()
        assert len(all_rows) == 1
        assert all_rows[0].cost == Decimal("100.00")


def test_sync_no_credential_records_reason(monkeypatch):
    with SessionLocal() as db:
        batch = sync_mod.sync_gmv_max(db, today=date(2026, 5, 12))
        assert batch.status == ImportBatchStatus.FAILED
        assert "not connected" in (batch.error_message or "").lower()
        assert db.query(GmvMaxDailyMetric).count() == 0


def test_sync_no_campaigns_completes_zero(monkeypatch):
    with SessionLocal() as db:
        _connect(db)
        _stub_api(monkeypatch, campaigns=[], stores=[], report_rows=[])
        batch = sync_mod.sync_gmv_max(db, today=date(2026, 5, 12))
        assert batch.status == ImportBatchStatus.COMPLETED
        assert batch.rows_imported == 0
        assert "no gmv-max campaigns" in (batch.error_message or "").lower()
