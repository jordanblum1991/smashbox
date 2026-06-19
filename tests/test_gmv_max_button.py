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
