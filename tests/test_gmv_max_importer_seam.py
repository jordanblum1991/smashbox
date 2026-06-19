"""The GMV-Max importer exposes a DataFrame seam (import_dataframe) shared by the
CSV run() path and the API sync. Normalized columns: metric_date / cost /
sku_orders / gross_revenue. Idempotent upsert by metric_date."""
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from app.db import Base, SessionLocal, engine
from app.importers.gmv_max_campaign import import_dataframe
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.gmv_max_daily_metric import GmvMaxDailyMetric


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _batch(db):
    b = ImportBatch(kind=ImportFileKind.TIKTOK_GMV_MAX, status=ImportBatchStatus.PROCESSING,
                    original_filename="api", stored_path="")
    db.add(b); db.flush()
    return b


def test_import_dataframe_inserts_rows():
    with SessionLocal() as db:
        b = _batch(db)
        df = pd.DataFrame([
            {"metric_date": date(2026, 5, 10), "cost": Decimal("100.00"),
             "sku_orders": 5, "gross_revenue": Decimal("300.00")},
            {"metric_date": date(2026, 5, 11), "cost": Decimal("50.00"),
             "sku_orders": 2, "gross_revenue": Decimal("120.00")},
        ])
        res = import_dataframe(df, db, b)
        db.commit()
        assert res.rows_imported == 2
        rows = db.query(GmvMaxDailyMetric).order_by(GmvMaxDailyMetric.metric_date).all()
        assert [(r.metric_date, r.cost, r.sku_orders, r.gross_revenue) for r in rows] == [
            (date(2026, 5, 10), Decimal("100.00"), 5, Decimal("300.00")),
            (date(2026, 5, 11), Decimal("50.00"), 2, Decimal("120.00")),
        ]


def test_import_dataframe_upserts_same_day():
    with SessionLocal() as db:
        b = _batch(db)
        import_dataframe(pd.DataFrame([
            {"metric_date": date(2026, 5, 10), "cost": Decimal("100.00"),
             "sku_orders": 5, "gross_revenue": Decimal("300.00")}]), db, b)
        db.commit()
        b2 = _batch(db)
        import_dataframe(pd.DataFrame([
            {"metric_date": date(2026, 5, 10), "cost": Decimal("111.00"),
             "sku_orders": 7, "gross_revenue": Decimal("333.00")}]), db, b2)
        db.commit()
        rows = db.query(GmvMaxDailyMetric).all()
        assert len(rows) == 1
        assert rows[0].cost == Decimal("111.00")
        assert rows[0].sku_orders == 7
        assert rows[0].import_batch_id == b2.id


def test_import_dataframe_empty_frame():
    with SessionLocal() as db:
        b = _batch(db)
        res = import_dataframe(pd.DataFrame([]), db, b)
        assert res.rows_imported == 0
        assert res.rows_skipped == 0


def test_import_dataframe_rejects_missing_columns():
    with SessionLocal() as db:
        b = _batch(db)
        with pytest.raises(ValueError, match="missing required columns"):
            import_dataframe(pd.DataFrame([{"metric_date": None}]), db, b)
