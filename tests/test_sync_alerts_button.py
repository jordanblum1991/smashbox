"""The scheduler runs the alert check after each job; the manual test button
sends a test email."""
import pytest
from fastapi.testclient import TestClient

import app.services.scheduler as sched
from app.db import Base, engine
from app.main import app


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def test_tiktok_job_runs_alert_check(monkeypatch):
    calls = []
    monkeypatch.setattr("app.services.tiktok_api.get_credential", lambda db: None)
    monkeypatch.setattr("app.services.sync_alerts.run_alert_check",
                        lambda db: calls.append("checked"))
    sched._run_tiktok_sync_job()
    assert "checked" in calls


def test_inventory_job_runs_alert_check(monkeypatch):
    calls = []
    monkeypatch.setattr("app.services.inventory_sync.sync_inventory_from_sap",
                        lambda db, source="scheduled": None)
    monkeypatch.setattr("app.services.gmv_max_sync.sync_gmv_max", lambda db: None)
    monkeypatch.setattr("app.services.sync_alerts.run_alert_check",
                        lambda db: calls.append("checked"))
    sched._run_inventory_sync_job()
    assert "checked" in calls


def test_alert_check_failure_does_not_abort_job(monkeypatch):
    monkeypatch.setattr("app.services.inventory_sync.sync_inventory_from_sap",
                        lambda db, source="scheduled": None)
    monkeypatch.setattr("app.services.gmv_max_sync.sync_gmv_max", lambda db: None)
    def boom(db):
        raise RuntimeError("alert check broke")
    monkeypatch.setattr("app.services.sync_alerts.run_alert_check", boom)
    sched._run_inventory_sync_job()    # must NOT raise


def test_test_button_sends_email(monkeypatch):
    sent = []
    monkeypatch.setattr("app.services.mailer.send_email",
                        lambda subject, body, *, to: sent.append(subject))
    # Make sync_alerts_enabled True so the endpoint actually sends.
    from app.config import settings
    monkeypatch.setattr(settings, "smtp_host", "h", raising=False)
    monkeypatch.setattr(settings, "smtp_user", "u", raising=False)
    monkeypatch.setattr(settings, "smtp_password", "pw", raising=False)
    monkeypatch.setattr(settings, "sync_alert_to", "a@x.com", raising=False)
    r = TestClient(app).post("/admin/sync-alerts/test", follow_redirects=False)
    assert r.status_code == 303
    assert sent and "test" in sent[0].lower()
