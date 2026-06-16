"""Action Center — consolidated open-items hub. Verifies the roll-up reflects
the underlying signals and that informational ("heads up") items stay out of the
actionable headline count."""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import (
    ImportBatch,
    ImportBatchStatus,
    ImportFileKind,
    Order,
    OrderLine,
    OrderType,
)
from app.models.import_batch import _utc_now_naive
from app.models.purchase_order import PurchaseOrder, PurchaseOrderLine
from app.models.shop import Shop
from app.models.sku import Sku
from app.models.tiktok_credential import TikTokCredential
from app.models.tiktok_daily_metric import TikTokDailyMetric
from app.models.tiktok_sync_state import TikTokSyncState
from app.reports.action_center import compute_action_center
from app.reports.inventory_alerts import _reset_cache
from app.reports.reconciliation import _reset_recon_cache


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    _reset_cache()  # inventory alert summary is process-cached
    _reset_recon_cache()  # recon-break summary is process-cached too
    with SessionLocal() as db:
        db.add(Shop(slug="smashbox", name="Smashbox", timezone="America/Los_Angeles"))
        db.add(Sku(sku="SBX-001", name="Primer", brand="smashbox",
                   tiktok_sku_id="SBX-001", unit_cogs=Decimal("5.00")))
        db.commit()
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_all_clear_when_empty(client):
    with SessionLocal() as db:
        v = compute_action_center(db)
        assert v.total_items == 0
        assert v.groups == []
        assert v.heads_up == []
    r = client.get("/action-center")
    assert r.status_code == 200
    assert "Action Center" in r.text
    assert "all caught up" in r.text


def test_unmapped_sku_is_a_data_health_item(client):
    with SessionLocal() as db:
        b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS,
                        status=ImportBatchStatus.COMPLETED,
                        original_filename="o.csv", stored_path="/tmp/o.csv")
        db.add(b)
        db.flush()
        o = Order(import_batch_id=b.id, tiktok_order_id="O-1",
                  placed_at=datetime(2026, 5, 1), order_type=OrderType.PAID,
                  status="Completed", brand="smashbox")
        db.add(o)
        db.flush()
        db.add(OrderLine(order_id=o.id, sku="UNMAPPED-XYZ", quantity=2))
        db.commit()
    with SessionLocal() as db:
        v = compute_action_center(db)
        keys = {it.key for g in v.groups for it in g.items}
        assert "dh_unmapped" in keys
        assert v.total_items >= 1
    r = client.get("/action-center")
    assert r.status_code == 200
    assert "unmapped" in r.text.lower()


def test_placed_po_is_heads_up_not_counted(client):
    with SessionLocal() as db:
        po = PurchaseOrder(number="PO-0001", supplier="S", status="placed")
        po.lines.append(PurchaseOrderLine(sku="SBX-001", name="Primer",
                                          quantity=10, unit_cost=Decimal("5")))
        db.add(po)
        db.commit()
    with SessionLocal() as db:
        v = compute_action_center(db)
        assert "open_pos" in {it.key for it in v.heads_up}
        # informational items must NOT inflate the actionable headline
        assert all(it.key != "open_pos" for g in v.groups for it in g.items)


def test_nav_badge_and_link_present(client):
    # Empty DB → link present, no badge number.
    r = client.get("/action-center")
    assert 'href="/action-center"' in r.text


# --- TikTok auto-sync health -----------------------------------------------

def _connect(db, *, cipher="CIPHER"):
    db.add(TikTokCredential(access_token="a", refresh_token="r", shop_cipher=cipher))


def _keys(view):
    return {i.key for g in view.groups for i in g.items}


def test_tiktok_sync_error_surfaces_as_error_item():
    with SessionLocal() as db:
        _connect(db)
        db.add(TikTokSyncState(stream="settlements", last_status="error", last_run_at=_utc_now_naive()))
        db.add(TikTokSyncState(stream="orders", last_status="ok", last_run_at=_utc_now_naive()))
        db.commit()
        v = compute_action_center(db)
    items = {i.key: i for g in v.groups for i in g.items}
    assert "tiktok_sync_error" in items
    assert items["tiktok_sync_error"].severity == "error"


def test_tiktok_sync_stale_when_last_run_old():
    with SessionLocal() as db:
        _connect(db)
        db.add(TikTokSyncState(stream="orders", last_status="ok",
                               last_run_at=_utc_now_naive() - timedelta(hours=40)))
        db.commit()
        v = compute_action_center(db)
    assert "tiktok_sync_stale" in _keys(v)


def test_tiktok_sync_healthy_is_silent():
    with SessionLocal() as db:
        _connect(db)
        db.add(TikTokSyncState(stream="orders", last_status="ok", last_run_at=_utc_now_naive()))
        db.add(TikTokSyncState(stream="analytics", last_status="empty", last_run_at=_utc_now_naive()))
        db.commit()
        v = compute_action_center(db)
    keys = _keys(v)
    assert "tiktok_sync_error" not in keys and "tiktok_sync_stale" not in keys


def test_no_sync_item_when_not_connected():
    """A credential without a shop_cipher isn't 'connected' — no sync flags."""
    with SessionLocal() as db:
        db.add(TikTokCredential(access_token="a", refresh_token="r"))  # no shop_cipher
        db.add(TikTokSyncState(stream="orders", last_status="error", last_run_at=_utc_now_naive()))
        db.commit()
        v = compute_action_center(db)
    assert "tiktok_sync_error" not in _keys(v)


# --- reconciliation breaks --------------------------------------------------

def _make_paid_order(db, *, batch_id, day, gross):
    """A PAID order on a given Pacific day (noon → safe from the tz boundary)."""
    db.add(Order(
        import_batch_id=batch_id, tiktok_order_id=f"RB-{day}-{gross}",
        placed_at=datetime(day.year, day.month, day.day, 12, 0),
        order_type=OrderType.PAID, status="Completed", brand="smashbox",
        gross_sales=Decimal(str(gross)),
    ))


def test_recon_break_surfaces_on_settled_day_mismatch():
    from datetime import timedelta
    from app.services.reporting_tz import today_local
    day = today_local() - timedelta(days=14)  # settled (past 10-day grace), within lookback
    with SessionLocal() as db:
        b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                        original_filename="o", stored_path="o")
        db.add(b); db.flush()
        _make_paid_order(db, batch_id=b.id, day=day, gross=100)        # our GMV 100
        db.add(TikTokDailyMetric(import_batch_id=b.id, metric_date=day, gmv=Decimal("50")))
        db.commit()
        v = compute_action_center(db)
    assert "dh_recon_break" in _keys(v)


def test_recon_break_ignores_recent_provisional_days():
    from datetime import timedelta
    from app.services.reporting_tz import today_local
    day = today_local() - timedelta(days=1)  # inside the settle-grace window
    with SessionLocal() as db:
        b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                        original_filename="o", stored_path="o")
        db.add(b); db.flush()
        _make_paid_order(db, batch_id=b.id, day=day, gross=100)
        db.add(TikTokDailyMetric(import_batch_id=b.id, metric_date=day, gmv=Decimal("50")))
        db.commit()
        v = compute_action_center(db)
    assert "dh_recon_break" not in _keys(v)


def test_no_recon_break_when_settled_day_ties():
    from datetime import timedelta
    from app.services.reporting_tz import today_local
    day = today_local() - timedelta(days=14)
    with SessionLocal() as db:
        b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                        original_filename="o", stored_path="o")
        db.add(b); db.flush()
        _make_paid_order(db, batch_id=b.id, day=day, gross=100)
        db.add(TikTokDailyMetric(import_batch_id=b.id, metric_date=day, gmv=Decimal("100")))  # ties
        db.commit()
        v = compute_action_center(db)
    assert "dh_recon_break" not in _keys(v)
