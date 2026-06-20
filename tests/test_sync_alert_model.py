"""SyncAlert persists per-condition alert state."""
import pytest

from app.db import Base, SessionLocal, engine
from app.models.sync_alert import SyncAlert


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def test_sync_alert_roundtrip():
    with SessionLocal() as db:
        db.add(SyncAlert(key="tiktok:settlements", state="alerting", message="boom"))
        db.commit()
        row = db.query(SyncAlert).filter_by(key="tiktok:settlements").one()
        assert row.state == "alerting"
        assert row.message == "boom"
        assert row.last_transition_at is not None


def test_sync_alert_in_metadata():
    assert "sync_alerts" in Base.metadata.tables
