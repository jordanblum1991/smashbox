"""CSV download endpoints for the Ad Spend, Reconciliation, and Data Health pages."""
import csv
import io

import pytest
from fastapi.testclient import TestClient

from app.db import Base, engine
from app.main import app


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _rows(r):
    return list(csv.reader(io.StringIO(r.text)))


def test_ad_spend_csv(client: TestClient):
    r = client.get("/reports/ad-spend.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert _rows(r)[0] == [
        "Year", "Month", "Gross Spend (GMV-Max)", "Blended ROAS", "SKU Orders",
        "Cost per Order", "Gross Revenue", "Attributed ROAS",
    ]


def test_reconciliation_csv(client: TestClient):
    r = client.get("/reports/reconciliation.csv?year=2026&month=5")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert _rows(r)[0] == [
        "Date", "Our GMV", "TikTok GMV", "Variance", "Refunds",
        "Net Customer Sales", "Orders",
    ]


def test_data_health_csv(client: TestClient):
    r = client.get("/reports/data-health.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert _rows(r)[0] == ["Issue Type", "Identifier", "Detail", "Amount", "Date"]


def test_data_health_csv_includes_missing_cogs(client: TestClient):
    """Missing COGS is the 4th Data Health section; it must appear in the CSV
    too (it was previously dropped from the export)."""
    from decimal import Decimal

    from app.db import SessionLocal
    from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
    from app.models.inventory_snapshot import InventorySnapshot
    from app.models.sku import Sku

    with SessionLocal() as db:
        db.add(Sku(sku="SBX-NOCOGS", name="No Cost Item", brand="smashbox",
                   tiktok_sku_id="SBX-NOCOGS", unit_cogs=Decimal("0")))
        b = ImportBatch(kind=ImportFileKind.INVENTORY_SNAPSHOT,
                        status=ImportBatchStatus.COMPLETED,
                        original_filename="s", stored_path="s")
        db.add(b)
        db.flush()
        db.add(InventorySnapshot(import_batch_id=b.id, sku="SBX-NOCOGS", on_hand=12,
                                 captured_at=__import__("datetime").datetime(2026, 6, 1)))
        db.commit()

    r = client.get("/reports/data-health.csv")
    rows = _rows(r)
    missing = [row for row in rows if row and row[0] == "Missing COGS"]
    assert len(missing) == 1
    assert missing[0][1] == "SBX-NOCOGS"
