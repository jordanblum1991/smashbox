"""TikTok Marketing API ad-spend integration — importer seam, credential store,
sync orchestration, fetcher mapping, and the connect/callback routes."""
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.ad_spend import AdSpend
from app.models.import_batch import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.shop import Shop
from app.models.tiktok_marketing_credential import TikTokMarketingCredential
from app.services import tiktok_marketing_api as mkt


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        db.add(Shop(slug="smashbox", name="Smashbox", timezone="America/Los_Angeles"))
        db.commit()
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _batch(db):
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ADS, status=ImportBatchStatus.COMPLETED,
                    original_filename="(api)", stored_path="(api)")
    db.add(b)
    db.flush()
    return b


# --- importer seam ----------------------------------------------------------

def test_import_ad_spend_rows_upserts_idempotently():
    from app.importers.tiktok_ads import import_ad_spend_rows
    rows = [
        {"spend_date": datetime(2026, 6, 1), "campaign_id": "C1", "campaign_name": "Camp 1",
         "amount": Decimal("12.34"), "cash_cost": Decimal("12.34")},
        {"spend_date": datetime(2026, 6, 2), "campaign_id": "C1", "campaign_name": "Camp 1",
         "amount": Decimal("5.00"), "cash_cost": Decimal("5.00")},
    ]
    with SessionLocal() as db:
        res = import_ad_spend_rows(rows, db, _batch(db))
        db.commit()
        assert res.rows_imported == 2
        assert db.query(AdSpend).count() == 2
        assert db.query(AdSpend).filter_by(campaign_id="C1").first().currency == "USD"

    # Re-import the same window with a corrected amount → upsert, no new rows.
    rows[0]["amount"] = Decimal("99.99")
    with SessionLocal() as db:
        import_ad_spend_rows(rows, db, _batch(db))
        db.commit()
        assert db.query(AdSpend).count() == 2
        d1 = db.query(AdSpend).filter_by(spend_date=datetime(2026, 6, 1)).one()
        assert d1.amount == Decimal("99.99")


# --- credential store --------------------------------------------------------

def test_store_credential_upserts_single_row():
    with SessionLocal() as db:
        mkt.store_credential(db, {"access_token": "tok", "advertiser_ids": ["123", "456"],
                                  "scope": [4, 5]},
                             [{"advertiser_id": "123", "advertiser_name": "Smashbox Ads"}])
        db.commit()
        cred = mkt.get_credential(db)
        assert cred.access_token == "tok"
        assert cred.advertiser_id == "123"
        assert cred.advertiser_ids == "123,456"
        assert cred.advertiser_name == "Smashbox Ads"
        assert mkt.advertiser_id_list(cred) == ["123", "456"]

    with SessionLocal() as db:
        mkt.store_credential(db, {"access_token": "tok2"}, None)  # token refresh
        db.commit()
        assert db.query(TikTokMarketingCredential).count() == 1
        assert mkt.get_credential(db).access_token == "tok2"


# --- fetcher mapping ---------------------------------------------------------

def test_fetch_ad_spend_maps_report_rows(monkeypatch):
    from app.services import tiktok_fetchers
    monkeypatch.setattr(mkt, "get_ad_spend", lambda token, aid, start, end: [
        {"campaign_id": "C9", "campaign_name": "Promo", "stat_day": "2026-06-10", "spend": Decimal("40.00")},
        {"campaign_id": "C9", "campaign_name": "Promo", "stat_day": "2026-06-11", "spend": Decimal("0")},
    ])
    with SessionLocal() as db:
        cred = TikTokMarketingCredential(access_token="t", advertiser_ids="123")
        db.add(cred)
        db.flush()
        n = tiktok_fetchers.fetch_ad_spend(db, cred, None)
        db.commit()
        assert n == 2
        row = db.query(AdSpend).filter_by(spend_date=datetime(2026, 6, 10)).one()
        assert row.amount == Decimal("40.00") and row.campaign_name == "Promo"


# --- sync orchestration ------------------------------------------------------

def test_run_ads_sync_pending_when_not_connected():
    with SessionLocal() as db:
        assert mkt.run_ads_sync(db) == "pending"
    with SessionLocal() as db:
        from app.models.tiktok_sync_state import TikTokSyncState
        st = db.query(TikTokSyncState).filter_by(stream="ads").one()
        assert st.last_status == "pending" and st.last_run_at is not None


def test_run_ads_sync_ok_when_connected(monkeypatch):
    from app.services import tiktok_fetchers
    monkeypatch.setattr(tiktok_fetchers, "fetch_ad_spend", lambda db, cred, since: 7)
    with SessionLocal() as db:
        db.add(TikTokMarketingCredential(access_token="tok", advertiser_ids="123"))
        db.commit()
    with SessionLocal() as db:
        assert mkt.run_ads_sync(db) == "ok"
        from app.models.tiktok_sync_state import TikTokSyncState
        st = db.query(TikTokSyncState).filter_by(stream="ads").one()
        assert st.rows_last_run == 7 and st.synced_through is not None


# --- routes ------------------------------------------------------------------

def test_status_page_renders(client):
    r = client.get("/admin/tiktok-ads")
    assert r.status_code == 200
    assert "Ad Spend" in r.text


def test_sync_now_also_runs_gmv_max(client, monkeypatch):
    """The 'Sync ad spend now' button refreshes BOTH feeds: the AdSpend cost
    export AND the GMV-Max daily metrics the Ad Spend report reads. Otherwise the
    button named after the report wouldn't actually update it (the trap that led
    to this change)."""
    import app.services.gmv_max_sync as gmv

    calls: list[str] = []
    monkeypatch.setattr(
        mkt, "run_ads_sync",
        lambda db, source="manual": (calls.append("ads"), "ok")[1],
    )

    def fake_gmv(db, **kw):
        calls.append("gmv")
        b = ImportBatch(kind=ImportFileKind.TIKTOK_GMV_MAX,
                        status=ImportBatchStatus.COMPLETED,
                        original_filename="(api)", stored_path="")
        b.rows_imported = 5
        db.add(b)
        db.flush()
        return b

    monkeypatch.setattr(gmv, "sync_gmv_max", fake_gmv)

    r = client.post("/admin/tiktok-ads/sync", follow_redirects=False)
    assert r.status_code == 303
    assert calls == ["ads", "gmv"]      # both feeds refreshed, in order


def test_authorize_requires_config(client):
    # Not configured → redirect back with an error (no crash).
    r = client.get("/auth/tiktok-ads/authorize", follow_redirects=False)
    assert r.status_code == 303
    assert "/admin/tiktok-ads" in r.headers["location"]


def test_callback_stores_credential(client, monkeypatch):
    monkeypatch.setattr(settings, "tiktok_marketing_app_id", "app")
    monkeypatch.setattr(settings, "tiktok_marketing_secret", "secret")
    monkeypatch.setattr(mkt, "exchange_auth_code",
                        lambda code: {"access_token": "TOK", "advertiser_ids": ["999"], "scope": [4]})
    monkeypatch.setattr(mkt, "get_advertisers",
                        lambda token: [{"advertiser_id": "999", "advertiser_name": "Acme"}])
    r = client.get("/auth/tiktok-ads/callback?auth_code=XYZ", follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        cred = mkt.get_credential(db)
        assert cred is not None and cred.access_token == "TOK"
        assert cred.advertiser_id == "999" and cred.advertiser_name == "Acme"
