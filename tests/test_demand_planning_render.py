"""Rendering smoke test for /reports/demand-planning.

Verifies the strip_size + title_case filter pass applied in the cleanup
pass: an all-caps source name with a trailing parenthesized size should
render title-cased and size-stripped, matching the treatment on
/admin/skus.
"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.import_batch import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.inventory_snapshot import InventorySnapshot
from app.models.sku import Sku


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_demand_planning_renders_product_name_title_cased(client: TestClient):
    """Source name is all-caps with a trailing size paren (the shape every
    TikTok master sheet row arrives in). After strip_size + title_case the
    page should display the human-readable form, NOT the raw source."""
    tiktok_id = "9000000000000001234"
    raw_name = "HALO SCULPT + GLOW FACE PALETTE - PINK SATURATION (15.7G/0.55OZ)"

    with SessionLocal() as db:
        batch = ImportBatch(
            kind=ImportFileKind.INVENTORY_SNAPSHOT,
            status=ImportBatchStatus.COMPLETED,
            original_filename="seed.csv",
            stored_path="/tmp/seed.csv",
        )
        db.add(batch)
        db.flush()
        db.add(Sku(
            sku="SBX-HALO-PINK",
            tiktok_sku_id=tiktok_id,
            name=raw_name,
            brand="Smashbox",
        ))
        db.add(InventorySnapshot(
            import_batch_id=batch.id,
            sku=tiktok_id,
            on_hand=120,
            captured_at=datetime(2026, 5, 25, 12, 0, 0),
        ))
        db.commit()

    r = client.get("/reports/demand-planning")
    assert r.status_code == 200, r.text[:300]

    # Title-cased + size-stripped form is what the cleanup pass renders.
    assert "Halo Sculpt" in r.text
    # The raw all-caps source must NOT appear — proves the filter chain ran.
    assert "HALO SCULPT" not in r.text
    # Trailing size paren must be gone too.
    assert "(15.7G/0.55OZ)" not in r.text
