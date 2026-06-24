"""Sample-report email settings + send-now routes. Admin-guarded; settings persist
on Shop + reschedule; send-now invokes the send seam; the card renders."""
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
    from app.auth import require_admin
    from app.main import app
    app.dependency_overrides[require_admin] = lambda: None
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(require_admin, None)


def test_settings_persist_period_days_and_enabled(monkeypatch, client):
    calls = {}
    monkeypatch.setattr(reports_mod, "apply_sample_report_schedule",
                        lambda shop: calls.setdefault("rescheduled", True))
    resp = client.post("/reports/samples/email-settings", data={
        "recipients": "a@x.com, b@x.com", "enabled": "1",
        "period": "mtd", "days": ["mon", "thu"], "report_time": "08:30"},
        follow_redirects=False)
    assert resp.status_code == 303
    with SessionLocal() as db:
        shop = db.query(Shop).first()
        assert shop.sample_report_enabled is True
        assert shop.sample_report_days == "mon,thu"
        assert shop.sample_report_period == "mtd"
        assert shop.sample_report_hour == 8 and shop.sample_report_minute == 30
        assert shop.sample_report_recipients_list == ["a@x.com", "b@x.com"]
    assert calls.get("rescheduled")


def test_invalid_period_falls_back_to_prev_month(monkeypatch, client):
    monkeypatch.setattr(reports_mod, "apply_sample_report_schedule", lambda shop: None)
    # 'last_7' is valid for SALES but NOT for SAMPLES → must fall back.
    client.post("/reports/samples/email-settings", data={
        "recipients": "a@x.com", "enabled": "1",
        "period": "last_7", "days": ["mon"], "report_time": "08:00"},
        follow_redirects=False)
    with SessionLocal() as db:
        assert db.query(Shop).first().sample_report_period == "prev_month"


def test_send_now_invokes_send(monkeypatch, client):
    from app.services import sample_report_email as sre
    sent = {}
    monkeypatch.setattr(sre.mailer, "send_email",
                        lambda *a, **k: sent.setdefault("called", True))
    with SessionLocal() as db:
        shop = db.query(Shop).first()
        shop.sample_report_recipients = "ops@x.com"; db.commit()
    resp = client.post("/reports/samples/send-now",
                       data={"period": "month"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/reports/samples?sent=ok"
    assert sent.get("called")


def test_send_now_no_recipients(client):
    resp = client.post("/reports/samples/send-now",
                       data={"period": "month"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/reports/samples?err=no-recipients"


def test_samples_page_renders_card(client):
    resp = client.get("/reports/samples")
    assert resp.status_code == 200
    assert "Email Sample report" in resp.text
    assert 'action="/reports/samples/email-settings"' in resp.text
    assert 'action="/reports/samples/send-now"' in resp.text
