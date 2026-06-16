"""TikTok auto-sync scheduler job — connection guard.

The daily job is registered up-front (so it starts working the moment the shop
authorizes), so it must no-op cleanly until a shop_cipher exists, and run the
full sync once connected.
"""
import pytest

from app.db import Base, SessionLocal, engine
from app.models.tiktok_credential import TikTokCredential
from app.services import scheduler


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _patch_run_sync(monkeypatch):
    calls = []
    from app.services import tiktok_sync
    monkeypatch.setattr(tiktok_sync, "run_sync", lambda db, **k: calls.append(k) or {})
    return calls


def test_job_skips_when_no_credential(monkeypatch):
    calls = _patch_run_sync(monkeypatch)
    scheduler._run_tiktok_sync_job()
    assert calls == []


def test_job_skips_when_connected_but_no_shop_cipher(monkeypatch):
    calls = _patch_run_sync(monkeypatch)
    with SessionLocal() as db:
        db.add(TikTokCredential(access_token="a", refresh_token="r"))  # no shop_cipher
        db.commit()
    scheduler._run_tiktok_sync_job()
    assert calls == []


def test_job_runs_sync_when_connected(monkeypatch):
    calls = _patch_run_sync(monkeypatch)
    with SessionLocal() as db:
        db.add(TikTokCredential(access_token="a", refresh_token="r", shop_cipher="CIPHER"))
        db.commit()
    scheduler._run_tiktok_sync_job()
    assert len(calls) == 1
    assert calls[0].get("source") == "scheduled"
