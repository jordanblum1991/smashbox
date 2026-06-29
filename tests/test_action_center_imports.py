"""Action Center 'Imports & data' staleness routing — auto-synced sources point
to their sync page (not a CSV re-import); only manual sources say 're-import'."""
from datetime import timedelta

import pytest

from app.db import Base, SessionLocal, engine
from app.models.import_batch import (
    ImportBatch,
    ImportBatchStatus,
    ImportFileKind,
    _utc_now_naive,
)
from app.reports.action_center import compute_action_center
from app.reports.inventory_alerts import _reset_cache
from app.reports.reconciliation import _reset_recon_cache


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    _reset_cache()
    _reset_recon_cache()
    yield


def _stale(db, kind):
    db.add(ImportBatch(kind=kind, status=ImportBatchStatus.COMPLETED,
                       original_filename="x", stored_path="",
                       uploaded_at=_utc_now_naive() - timedelta(days=8)))
    db.commit()


def _find(view, key):
    return next((i for g in view.groups for i in g.items if i.key == key), None)


def test_stale_manual_source_says_reimport_via_uploads():
    with SessionLocal() as db:
        _stale(db, ImportFileKind.SAMPLES)
        v = compute_action_center(db)
    item = _find(v, "data_stale")
    assert item is not None
    assert item.href == "/uploads"
    assert "re-import" in item.detail.lower()


def test_stale_ad_spend_points_to_marketing_sync_not_uploads():
    with SessionLocal() as db:
        _stale(db, ImportFileKind.TIKTOK_GMV_MAX)
        v = compute_action_center(db)
    item = _find(v, "ad_spend_stale")
    assert item is not None
    assert item.href == "/admin/tiktok-ads"
    assert "re-import" not in item.detail.lower()
    # No CSV-re-import item for an auto-synced source.
    assert _find(v, "data_stale") is None


def test_stale_shop_api_source_not_flagged_for_csv_reimport():
    """Orders/Settlements/Payouts staleness is the TikTok-sync's job (covered
    elsewhere) — it must NOT produce a '/uploads re-import' item."""
    with SessionLocal() as db:
        _stale(db, ImportFileKind.TIKTOK_ORDERS)
        v = compute_action_center(db)
    assert _find(v, "data_stale") is None
    assert all(
        "/uploads" not in i.href
        for g in v.groups for i in g.items if "Orders" in i.detail
    )
