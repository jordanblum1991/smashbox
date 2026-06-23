"""Inventory-report email settings + send-now routes. Admin-guarded; settings
persist on Shop and reschedule; send-now invokes the send seam."""
import pytest
from starlette.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.models.shop import Shop
import app.routers.reports as reports_mod


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        db.add(Shop(slug="smashbox", name="Smashbox")); db.commit()
    yield


@pytest.fixture
def client():
    # Bypass admin auth cleanly via FastAPI dependency_overrides (monkeypatching
    # auth.require_admin would NOT affect the dependency already bound into the
    # route at import time).
    from app.auth import require_admin
    from app.main import app
    app.dependency_overrides[require_admin] = lambda: None
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(require_admin, None)


def test_settings_persist_and_reschedule(monkeypatch, client):
    calls = {}
    monkeypatch.setattr(reports_mod, "apply_inventory_report_schedule",
                        lambda shop: calls.setdefault("rescheduled", True))
    resp = client.post("/reports/inventory/email-settings", data={
        "recipients": "a@x.com, b@x.com", "enabled": "1",
        "days": ["mon", "thu"], "report_time": "08:30"},
        follow_redirects=False)
    assert resp.status_code == 303
    with SessionLocal() as db:
        shop = db.query(Shop).first()
        assert shop.inventory_report_enabled is True
        assert shop.inventory_report_days == "mon,thu"
        assert shop.inventory_report_hour == 8 and shop.inventory_report_minute == 30
        assert shop.report_recipients_list == ["a@x.com", "b@x.com"]
    assert calls.get("rescheduled")


def test_send_now_invokes_send(monkeypatch, client):
    sent = {}
    monkeypatch.setattr(reports_mod, "send_inventory_report",
                        lambda db, recipients: sent.setdefault("to", recipients))
    with SessionLocal() as db:
        shop = db.query(Shop).first()
        shop.inventory_report_recipients = "ops@x.com"; db.commit()
    resp = client.post("/reports/inventory/send-now", follow_redirects=False)
    assert resp.status_code == 303
    assert sent["to"] == ["ops@x.com"]
