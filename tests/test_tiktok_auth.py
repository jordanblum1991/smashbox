"""TikTok credential storage + signing — the testable (no-network) bits."""
import pytest

from app.config import settings
from app.db import Base, SessionLocal, engine
from app.models.import_batch import _utc_now_naive
from app.models.tiktok_credential import TikTokCredential
from app.services import tiktok_api


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def test_store_credential_upserts_single_row():
    data = {"access_token": "a1", "refresh_token": "r1", "seller_name": "Smashbox",
            "seller_base_region": "US", "granted_scopes": ["order", "finance"]}
    with SessionLocal() as db:
        tiktok_api.store_credential(db, data, {"id": "123", "cipher": "CIPHER", "name": "Smashbox Shop"})
        db.commit()
    with SessionLocal() as db:
        c = db.query(TikTokCredential).one()
        assert c.access_token == "a1"
        assert c.shop_cipher == "CIPHER" and c.shop_name == "Smashbox Shop"
        assert c.granted_scopes == "order,finance" and c.region == "US"
    # A later store (e.g. a refresh) updates the SAME row.
    with SessionLocal() as db:
        tiktok_api.store_credential(db, {"access_token": "a2", "refresh_token": "r2"}, None)
        db.commit()
    with SessionLocal() as db:
        assert db.query(TikTokCredential).count() == 1
        c = db.query(TikTokCredential).one()
        assert c.access_token == "a2" and c.shop_cipher == "CIPHER"  # shop info preserved


def test_expiry_handles_timestamp_and_duration():
    assert tiktok_api._expiry(2_000_000_000).year >= 2033   # absolute Unix ts
    assert tiktok_api._expiry(3600) > _utc_now_naive()       # seconds-from-now
    assert tiktok_api._expiry(None) is None


def test_signed_params_adds_sign(monkeypatch):
    monkeypatch.setattr(settings, "tiktok_app_key", "K")
    monkeypatch.setattr(settings, "tiktok_app_secret", "S")
    p = tiktok_api.signed_params("/authorization/202309/shops", {"shop_cipher": "C"})
    assert p["app_key"] == "K" and "timestamp" in p and len(p["sign"]) == 64  # sha256 hex
