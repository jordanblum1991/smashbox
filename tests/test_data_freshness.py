"""Data-freshness 'Ad spend' tracks the live GMV-Max auto-sync, not the
deprecated manual TIKTOK_ADS Cost upload."""
from datetime import timedelta

import pytest

from app.db import Base, SessionLocal, engine
from app.models.import_batch import (
    ImportBatch,
    ImportBatchStatus,
    ImportFileKind,
    _utc_now_naive,
)
from app.services.data_freshness import compute_freshness


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _batch(db, kind, *, age_days=0):
    db.add(ImportBatch(
        kind=kind, status=ImportBatchStatus.COMPLETED,
        original_filename="api", stored_path="",
        uploaded_at=_utc_now_naive() - timedelta(days=age_days),
    ))
    db.commit()


def test_ad_spend_freshness_tracks_gmv_max():
    with SessionLocal() as db:
        _batch(db, ImportFileKind.TIKTOK_GMV_MAX)        # fresh GMV-Max pull
        by = {f.label: f for f in compute_freshness(db)}
    assert by["Ad spend"].kind == ImportFileKind.TIKTOK_GMV_MAX
    assert by["Ad spend"].staleness == "fresh"


def test_legacy_tiktok_ads_upload_does_not_freshen_ad_spend():
    """A stale/absent GMV-Max sync shows 'Ad spend' as missing even if an old
    manual TIKTOK_ADS upload exists — we no longer track that source."""
    with SessionLocal() as db:
        _batch(db, ImportFileKind.TIKTOK_ADS, age_days=1)  # legacy upload, recent
        by = {f.label: f for f in compute_freshness(db)}
    assert by["Ad spend"].staleness == "missing"
