"""Manual GMV-Max sync button: posts off the event loop, 303s back to /uploads,
and the Uploads page shows a GMV-Max feed card."""
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
def client():
    return TestClient(app)


def test_uploads_page_shows_gmv_card(client):
    r = client.get("/uploads")
    assert r.status_code == 200
    assert "Live GMV-Max feed (TikTok API)" in r.text
    assert "/uploads/sync-gmv-max" in r.text


def test_sync_button_calls_service_and_redirects(client, monkeypatch):
    called = {}
    monkeypatch.setattr("app.routers.uploads.sync_gmv_max",
                        lambda db, **kw: called.setdefault("hit", True))
    r = client.post("/uploads/sync-gmv-max", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/uploads"
    assert called.get("hit") is True


def test_uploads_page_renders_last_gmv_sync_status(client):
    from datetime import datetime
    from app.db import SessionLocal
    from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
    with SessionLocal() as db:
        db.add(ImportBatch(
            kind=ImportFileKind.TIKTOK_GMV_MAX,
            status=ImportBatchStatus.COMPLETED,
            original_filename="TikTok GMV-Max API sync · 2026-06-19 06:10",
            stored_path="",
            rows_imported=35,
        ))
        db.commit()
    r = client.get("/uploads")
    assert r.status_code == 200
    assert "completed" in r.text
    # the populated status block shows the sync timestamp line
    assert "UTC" in r.text
