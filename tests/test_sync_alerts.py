"""evaluate_sync_alerts (conditions from sync state) + run_alert_check (the
edge-triggered email state machine). No network — mailer + evaluator stubbed."""
from datetime import timedelta

import pytest

import app.services.sync_alerts as sa
from app.config import settings
from app.db import Base, SessionLocal, engine
from app.models.import_batch import (
    ImportBatch, ImportBatchStatus, ImportFileKind, _utc_now_naive,
)
from app.models.shop import Shop
from app.models.sync_alert import SyncAlert
from app.models.tiktok_credential import TikTokCredential
from app.models.tiktok_marketing_credential import TikTokMarketingCredential
from app.models.tiktok_sync_state import TikTokSyncState
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


# --- error-alert gating decoupled by credential domain ----------------------

def _marketing_connected(db):
    db.add(TikTokMarketingCredential(access_token="t"))


def test_ads_error_alerts_without_shop_connection(monkeypatch):
    """A Marketing-API ('ads') sync error must alert even when the Shop API is
    disconnected — the two use separate credentials."""
    with SessionLocal() as db:
        _marketing_connected(db)                      # Marketing connected; NO Shop cred
        db.add(TikTokSyncState(stream="ads", last_status="error",
                               last_message="40105 invalid access token"))
        db.commit()
        keys = {c.key for c in sa.evaluate_sync_alerts(db)}
    assert "tiktok:ads" in keys


def test_ads_error_gated_on_marketing_not_shop():
    """With the Shop API connected but Marketing disconnected, an 'ads' error is
    NOT alerted (the feed can't run) — but a Shop stream error still is."""
    with SessionLocal() as db:
        _shop_connected(db)                           # Shop connected; NO Marketing cred
        db.add(TikTokSyncState(stream="ads", last_status="error", last_message="x"))
        db.add(TikTokSyncState(stream="orders", last_status="error", last_message="y"))
        db.commit()
        keys = {c.key for c in sa.evaluate_sync_alerts(db)}
    assert "tiktok:ads" not in keys
    assert "tiktok:orders" in keys


def _cond(conds, key):
    return next((c for c in conds if c.key == key), None)


def test_ads_auth_error_message_is_actionable():
    """A Marketing-API auth failure (revoked/invalid token — the only real
    'expiry' failure mode, since these tokens are long-lived) tells the operator
    to reconnect, not just a raw error string."""
    with SessionLocal() as db:
        _marketing_connected(db)
        db.add(TikTokSyncState(stream="ads", last_status="error",
                               last_message="TikTok Marketing API error 40105: Access token is invalid"))
        db.commit()
        c = _cond(sa.evaluate_sync_alerts(db), "tiktok:ads")
    assert c is not None and "reconnect the marketing api" in c.message.lower()


def test_gmv_auth_failure_message_is_actionable():
    with SessionLocal() as db:
        db.add(TikTokMarketingCredential(access_token="t"))
        db.add(Shop(slug="smashbox", name="Smashbox", timezone="America/Los_Angeles"))
        db.add(ImportBatch(kind=ImportFileKind.TIKTOK_GMV_MAX, status=ImportBatchStatus.FAILED,
                           original_filename="TikTok GMV-Max API sync", stored_path="",
                           error_message="GMV-Max API sync failed: Access token is invalid"))
        db.commit()
        c = _cond(sa.evaluate_sync_alerts(db), "gmv_max")
    assert c is not None and "reconnect the marketing api" in c.message.lower()


def test_non_auth_error_message_not_decorated():
    """A transient (non-auth) error keeps its plain message — no reconnect noise."""
    with SessionLocal() as db:
        _marketing_connected(db)
        db.add(TikTokSyncState(stream="ads", last_status="error",
                               last_message="read timeout after 40s"))
        db.commit()
        c = _cond(sa.evaluate_sync_alerts(db), "tiktok:ads")
    assert c is not None and "reconnect" not in c.message.lower()


# --- per-feed staleness -----------------------------------------------------

def _shop_connected(db):
    db.add(TikTokCredential(access_token="t", refresh_token="r", shop_cipher="c"))


def _completed_batch(db, kind, fname, *, age_days):
    db.add(ImportBatch(kind=kind, status=ImportBatchStatus.COMPLETED,
                       original_filename=fname, stored_path="",
                       completed_at=_utc_now_naive() - timedelta(days=age_days)))


def test_stale_shop_stream_alerts(monkeypatch):
    """A Shop stream whose last successful sync is older than the threshold
    alerts per-stream (not masked by other fresh streams)."""
    monkeypatch.setattr(settings, "tiktok_auto_sync_enabled", True, raising=False)
    with SessionLocal() as db:
        _shop_connected(db)
        db.add(TikTokSyncState(stream="orders", last_status="ok", last_run_at=_utc_now_naive(),
                               synced_through=_utc_now_naive() - timedelta(days=3)))
        db.commit()
        keys = {c.key for c in sa.evaluate_sync_alerts(db)}
    assert "stale:orders" in keys


def test_fresh_shop_stream_no_stale(monkeypatch):
    monkeypatch.setattr(settings, "tiktok_auto_sync_enabled", True, raising=False)
    with SessionLocal() as db:
        _shop_connected(db)
        db.add(TikTokSyncState(stream="orders", last_status="ok", last_run_at=_utc_now_naive(),
                               synced_through=_utc_now_naive() - timedelta(hours=1)))
        db.commit()
        keys = {c.key for c in sa.evaluate_sync_alerts(db)}
    assert "stale:orders" not in keys


def test_disabled_shop_streams_no_stale(monkeypatch):
    """When auto-sync is off, a stale stream is expected, not an alert."""
    monkeypatch.setattr(settings, "tiktok_auto_sync_enabled", False, raising=False)
    with SessionLocal() as db:
        _shop_connected(db)
        db.add(TikTokSyncState(stream="orders", last_status="ok",
                               synced_through=_utc_now_naive() - timedelta(days=5)))
        db.commit()
        keys = {c.key for c in sa.evaluate_sync_alerts(db)}
    assert "stale:orders" not in keys


def test_stale_requires_connection(monkeypatch):
    """No Shop credential → don't alert (the feed can't run anyway)."""
    monkeypatch.setattr(settings, "tiktok_auto_sync_enabled", True, raising=False)
    with SessionLocal() as db:
        db.add(TikTokSyncState(stream="orders", last_status="ok",
                               synced_through=_utc_now_naive() - timedelta(days=5)))
        db.commit()
        keys = {c.key for c in sa.evaluate_sync_alerts(db)}
    assert "stale:orders" not in keys


def test_stale_gmv_alerts():
    """GMV-Max staleness comes from the latest COMPLETED batch's age, gated on the
    Marketing connection + the gmv schedule — independent of the Shop credential."""
    with SessionLocal() as db:
        db.add(TikTokMarketingCredential(access_token="t"))
        db.add(Shop(slug="smashbox", name="Smashbox", timezone="America/Los_Angeles"))
        _completed_batch(db, ImportFileKind.TIKTOK_GMV_MAX, "TikTok GMV-Max API sync", age_days=3)
        db.commit()
        keys = {c.key for c in sa.evaluate_sync_alerts(db)}
    assert "stale:gmv_max" in keys


def test_stale_inventory_alerts():
    """SAP inventory tolerates a weekend gap but alerts past its longer threshold."""
    with SessionLocal() as db:
        db.add(Shop(slug="smashbox", name="Smashbox", timezone="America/Los_Angeles"))
        _completed_batch(db, ImportFileKind.INVENTORY_SNAPSHOT, "SAP SB+SBS sync", age_days=4)
        db.commit()
        keys = {c.key for c in sa.evaluate_sync_alerts(db)}
    assert "stale:inventory" in keys


def test_recent_inventory_no_stale():
    with SessionLocal() as db:
        db.add(Shop(slug="smashbox", name="Smashbox", timezone="America/Los_Angeles"))
        _completed_batch(db, ImportFileKind.INVENTORY_SNAPSHOT, "SAP SB+SBS sync", age_days=1)
        db.commit()
        keys = {c.key for c in sa.evaluate_sync_alerts(db)}
    assert "stale:inventory" not in keys


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
