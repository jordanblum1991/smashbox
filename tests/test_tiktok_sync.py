"""TikTok sync orchestration — state tracking + graceful 'pending' until the
live connection (and its fetchers) exist."""
from datetime import datetime

import pytest

from app.db import Base, SessionLocal, engine
from app.models.tiktok_credential import TikTokCredential
from app.services import tiktok_sync


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def test_all_states_creates_three_streams():
    with SessionLocal() as db:
        states = tiktok_sync.all_states(db)
        db.commit()
        assert {s.stream for s in states} == {"orders", "settlements", "payouts"}


def test_run_sync_pending_when_not_connected():
    with SessionLocal() as db:
        summary = tiktok_sync.run_sync(db)
        assert summary == {"orders": "pending", "settlements": "pending", "payouts": "pending"}
    with SessionLocal() as db:
        states = {s.stream: s for s in tiktok_sync.all_states(db)}
        assert states["orders"].last_status == "pending"
        assert states["orders"].last_run_at is not None
        assert "not connected" in states["orders"].last_message


def test_run_sync_orders_runs_settlements_payouts_pending(monkeypatch):
    """Orders has a live fetcher now; settlements/payouts are still unbuilt and
    record 'pending'. iter_orders is stubbed so the unit test never hits the API."""
    from app.services import tiktok_api

    monkeypatch.setattr(tiktok_api, "iter_orders", lambda *a, **k: iter(()))

    with SessionLocal() as db:
        db.add(TikTokCredential(access_token="a", refresh_token="r", shop_cipher="CIPHER",
                                access_expires_at=datetime(2030, 1, 1)))  # not near expiry → no refresh
        db.commit()
    with SessionLocal() as db:
        summary = tiktok_sync.run_sync(db)
        assert summary["orders"] == "empty"          # connected + ran, no rows
        assert summary["settlements"] == "pending"
        assert summary["payouts"] == "pending"
        states = {s.stream: s for s in tiktok_sync.all_states(db)}
        assert "fetcher is wired" in (states["settlements"].last_message or "")
        assert states["orders"].last_run_at is not None
