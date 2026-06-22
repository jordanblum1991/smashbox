"""evaluate_sync_alerts (conditions from sync state) + run_alert_check (the
edge-triggered email state machine). No network — mailer + evaluator stubbed."""
import pytest

import app.services.sync_alerts as sa
from app.config import settings
from app.db import Base, SessionLocal, engine
from app.models.import_batch import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.sync_alert import SyncAlert
from app.services.sync_alerts import AlertCondition, run_alert_check


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "h", raising=False)
    monkeypatch.setattr(settings, "smtp_user", "u", raising=False)
    monkeypatch.setattr(settings, "smtp_password", "pw", raising=False)
    monkeypatch.setattr(settings, "sync_alert_to", "a@x.com", raising=False)


def _failed_batch(db, kind, fname):
    db.add(ImportBatch(kind=kind, status=ImportBatchStatus.FAILED,
                       original_filename=fname, stored_path="", error_message="boom"))
    db.commit()


def test_evaluate_flags_failed_gmv_and_inventory_batches():
    with SessionLocal() as db:
        _failed_batch(db, ImportFileKind.TIKTOK_GMV_MAX, "TikTok GMV-Max API sync")
        _failed_batch(db, ImportFileKind.INVENTORY_SNAPSHOT, "SAP SB+SBS sync")
        conds = sa.evaluate_sync_alerts(db)
    keys = {c.key for c in conds}
    assert "gmv_max" in keys and "inventory" in keys


def test_evaluate_healthy_returns_none():
    with SessionLocal() as db:
        db.add(ImportBatch(kind=ImportFileKind.TIKTOK_GMV_MAX,
                           status=ImportBatchStatus.COMPLETED,
                           original_filename="TikTok GMV-Max API sync", stored_path=""))
        db.commit()
        assert sa.evaluate_sync_alerts(db) == []


def test_edge_trigger_failure_then_recovery(monkeypatch, enabled):
    sent = []
    monkeypatch.setattr(sa.mailer, "send_email",
                        lambda subject, body, *, to: sent.append(subject))
    conds = [AlertCondition("gmv_max", "GMV-Max sync failed", "boom")]
    monkeypatch.setattr(sa, "evaluate_sync_alerts", lambda db: list(conds))

    with SessionLocal() as db:
        run_alert_check(db)
        assert len(sent) == 1 and "alert" in sent[0].lower()
        run_alert_check(db)
        assert len(sent) == 1
        conds.clear()
        run_alert_check(db)
        assert len(sent) == 2 and "recover" in sent[1].lower()
        row = db.query(SyncAlert).filter_by(key="gmv_max").one()
        assert row.state == "ok"


def test_re_arms_after_recovery(monkeypatch, enabled):
    sent = []
    monkeypatch.setattr(sa.mailer, "send_email",
                        lambda subject, body, *, to: sent.append(subject))
    conds = [AlertCondition("gmv_max", "GMV-Max sync failed", "boom")]
    monkeypatch.setattr(sa, "evaluate_sync_alerts", lambda db: list(conds))
    with SessionLocal() as db:
        run_alert_check(db)
        conds.clear(); run_alert_check(db)
        conds.append(AlertCondition("gmv_max", "GMV-Max sync failed", "boom2"))
        run_alert_check(db)
    assert len(sent) == 3


def test_disabled_is_noop(monkeypatch):
    sent = []
    monkeypatch.setattr(sa.mailer, "send_email",
                        lambda subject, body, *, to: sent.append(subject))
    monkeypatch.setattr(sa, "evaluate_sync_alerts",
                        lambda db: [AlertCondition("gmv_max", "x", "y")])
    with SessionLocal() as db:
        run_alert_check(db)
    assert sent == []


def test_send_failure_does_not_advance_state(monkeypatch, enabled):
    def boom(subject, body, *, to):
        raise RuntimeError("smtp down")
    monkeypatch.setattr(sa.mailer, "send_email", boom)
    monkeypatch.setattr(sa, "evaluate_sync_alerts",
                        lambda db: [AlertCondition("gmv_max", "x", "y")])
    with SessionLocal() as db:
        run_alert_check(db)
        assert db.query(SyncAlert).filter_by(key="gmv_max", state="alerting").count() == 0
