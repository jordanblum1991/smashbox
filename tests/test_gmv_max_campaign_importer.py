"""GMV Max "Campaign overview" (By-Day) importer: one GmvMaxDailyMetric row per
day, footer TOTAL row skipped, idempotent on re-import.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pandas as pd
import pytest

from app.db import Base, SessionLocal, engine
from app.importers.gmv_max_campaign import GmvMaxCampaignImporter
from app.models.gmv_max_daily_metric import GmvMaxDailyMetric
from app.models.import_batch import ImportBatch, ImportBatchStatus, ImportFileKind

COLS = ["By Day", "Cost", "SKU orders (Current shop)", "Cost per order (Current shop)",
        "Gross revenue (Current shop)", "ROI (Current shop)", "Currency"]


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _batch(db):
    b = ImportBatch(kind=ImportFileKind.TIKTOK_GMV_MAX, status=ImportBatchStatus.COMPLETED,
                    original_filename="camp.xlsx", stored_path="camp.xlsx")
    db.add(b); db.flush()
    return b


def _write(path, rows, footer=True):
    data = list(rows)
    if footer:  # TikTok appends a TOTAL row with "-" in the By Day cell.
        data.append(["-", "150.00", "7", "21.43", "420.00", "2.80", "USD"])
    pd.DataFrame(data, columns=COLS).to_excel(path, index=False)


def test_imports_daily_rows_and_skips_footer(tmp_path):
    p = tmp_path / "camp.xlsx"
    _write(p, [
        [datetime(2026, 5, 1), "100.00", "5", "20.00", "300.00", "3.00", "USD"],
        [datetime(2026, 5, 2), "50.00", "2", "25.00", "120.00", "2.40", "USD"],
    ])
    with SessionLocal() as db:
        b = _batch(db)
        res = GmvMaxCampaignImporter().run(p, db, b)
        db.commit()
        assert res.rows_imported == 2          # footer "-" row skipped
        rows = db.query(GmvMaxDailyMetric).order_by(GmvMaxDailyMetric.metric_date).all()
        assert len(rows) == 2
        assert rows[0].metric_date == date(2026, 5, 1)
        assert rows[0].cost == Decimal("100.00")
        assert rows[0].sku_orders == 5
        assert rows[0].gross_revenue == Decimal("300.00")


def test_reimport_is_idempotent(tmp_path):
    p = tmp_path / "camp.xlsx"
    _write(p, [[datetime(2026, 5, 1), "100.00", "5", "20.00", "300.00", "3.00", "USD"]])
    with SessionLocal() as db:
        b = _batch(db)
        GmvMaxCampaignImporter().run(p, db, b); db.commit()
    # Re-upload the same day with revised values.
    _write(p, [[datetime(2026, 5, 1), "111.00", "6", "18.50", "333.00", "3.00", "USD"]])
    with SessionLocal() as db:
        b = _batch(db)
        GmvMaxCampaignImporter().run(p, db, b); db.commit()
        rows = db.query(GmvMaxDailyMetric).all()
        assert len(rows) == 1                  # upsert, not append
        assert rows[0].cost == Decimal("111.00")
        assert rows[0].sku_orders == 6
        assert rows[0].gross_revenue == Decimal("333.00")


def test_missing_required_columns_raises(tmp_path):
    p = tmp_path / "bad.xlsx"
    pd.DataFrame([[1, 2]], columns=["foo", "bar"]).to_excel(p, index=False)
    with SessionLocal() as db:
        b = _batch(db)
        with pytest.raises(ValueError):
            GmvMaxCampaignImporter().run(p, db, b)
