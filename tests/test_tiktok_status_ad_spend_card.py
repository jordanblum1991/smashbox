"""The Shop-API 'API Connection' page surfaces a compact ad-spend (Marketing API)
status card + link, so both connections are visible from one place."""
import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.shop import Shop
from app.models.tiktok_sync_state import TikTokSyncState
from app.services import tiktok_marketing_api as mkt


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        db.add(Shop(slug="smashbox", name="Smashbox", timezone="America/Los_Angeles"))
        db.commit()
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_api_connection_page_shows_ad_spend_card_and_link(client):
    r = client.get("/admin/tiktok")
    assert r.status_code == 200
    # The compact ad-spend card, clearly labeled as the (separate) Marketing API…
    assert "Ad spend" in r.text
    assert "Marketing API" in r.text
    # …with a link through to the full Marketing page.
    assert "/admin/tiktok-ads" in r.text


def test_ad_spend_card_shows_full_sync_detail(client):
    # The card shows the full sync detail (status, synced-through, last run, rows).
    # Distinctive values so the assertions can't be satisfied by the Shop streams.
    from datetime import datetime
    with SessionLocal() as db:
        db.add(TikTokSyncState(stream=mkt.ADS_STREAM, last_status="ok",
                               synced_through=datetime(2026, 6, 28, 9, 0),
                               last_run_at=datetime(2026, 6, 29, 7, 45),
                               rows_last_run=137))
        db.commit()
    r = client.get("/admin/tiktok")
    assert r.status_code == 200
    assert "2026-06-28 09:00" in r.text          # synced through
    assert "2026-06-29 07:45" in r.text          # last run
    assert "137" in r.text                        # rows synced
