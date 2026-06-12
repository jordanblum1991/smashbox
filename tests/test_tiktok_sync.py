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


def test_run_sync_pending_when_connected_but_fetchers_unbuilt():
    with SessionLocal() as db:
        db.add(TikTokCredential(access_token="a", refresh_token="r", shop_cipher="CIPHER",
                                access_expires_at=datetime(2030, 1, 1)))  # not near expiry → no refresh
        db.commit()
    with SessionLocal() as db:
        summary = tiktok_sync.run_sync(db)
        assert all(v == "pending" for v in summary.values())
        states = {s.stream: s for s in tiktok_sync.all_states(db)}
        assert "fetchers are wired" in (states["orders"].last_message or "")
