"""POST /admin/tiktok-ads/schedule — persist + live-reschedule the GMV-Max sync."""
import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.shop import Shop


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


def test_schedule_post_persists_and_reschedules(client: TestClient):
    r = client.post("/admin/tiktok-ads/schedule",
                    data={"sync_time": "08:15", "enabled": "on",
                          "days": ["mon", "wed", "fri"]},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/admin/tiktok-ads")
    with SessionLocal() as db:
        shop = db.query(Shop).first()
        assert shop.gmv_sync_enabled is True
        assert shop.gmv_sync_hour == 8 and shop.gmv_sync_minute == 15
        assert shop.gmv_sync_days == "mon,wed,fri"


def test_schedule_post_unchecked_enabled_disables(client: TestClient):
    """No 'enabled' checkbox → schedule is disabled."""
    r = client.post("/admin/tiktok-ads/schedule",
                    data={"sync_time": "09:00", "days": ["mon"]},
                    follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        assert db.query(Shop).first().gmv_sync_enabled is False


def test_tiktok_ads_page_renders_schedule_form(client: TestClient):
    r = client.get("/admin/tiktok-ads")
    assert r.status_code == 200
    assert 'action="/admin/tiktok-ads/schedule"' in r.text   # the schedule form
    assert 'name="days"' in r.text                            # day checkboxes


def test_schedule_post_bad_time_is_rejected(client: TestClient):
    r = client.post("/admin/tiktok-ads/schedule",
                    data={"sync_time": "25:99", "enabled": "on", "days": ["mon"]},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "error" in r.headers["location"]
    with SessionLocal() as db:
        # Unchanged from the default (7:45) — nothing half-saved.
        assert db.query(Shop).first().gmv_sync_hour == 7
