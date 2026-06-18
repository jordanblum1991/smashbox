"""Ad Budget tracking — running available = budget − GMV-Max spend − dated
promotions. Covers the computation (boundaries, promotions, not-started,
over-budget, today-clamping), current_budget selection, and the CRUD/CSV routes.
"""
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.ad_budget import AdBudget, AdBudgetPromotion
from app.models.gmv_max_daily_metric import GmvMaxDailyMetric
from app.reports.ad_budget import compute_budget_view, current_budget, list_budgets


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _batch(db):
    b = ImportBatch(kind=ImportFileKind.TIKTOK_GMV_MAX, status=ImportBatchStatus.COMPLETED,
                    original_filename="f", stored_path="f")
    db.add(b); db.flush()
    return b


def _spend(db, bid, d: date, cost):
    db.add(GmvMaxDailyMetric(import_batch_id=bid, metric_date=d,
                             cost=Decimal(str(cost)), sku_orders=0, gross_revenue=Decimal("0")))


def _budget(db, start, end, amount, label="Test"):
    b = AdBudget(label=label, start_date=start, end_date=end, amount=Decimal(str(amount)))
    db.add(b); db.flush()
    return b


# ---- Computation -----------------------------------------------------------

def test_running_available_spend_promotions_and_boundaries():
    with SessionLocal() as db:
        bch = _batch(db)
        b = _budget(db, date(2026, 7, 1), date(2026, 7, 31), 10000)
        _spend(db, bch.id, date(2026, 6, 30), 999)   # before start → excluded
        _spend(db, bch.id, date(2026, 7, 1), 100)    # in
        _spend(db, bch.id, date(2026, 7, 5), 200)    # in
        _spend(db, bch.id, date(2026, 7, 11), 999)   # after `today` → excluded
        db.add(AdBudgetPromotion(ad_budget_id=b.id, name="Spring", amount=Decimal("500"),
                                 promo_date=date(2026, 7, 3)))
        db.commit()
        b = db.get(AdBudget, b.id)
        v = compute_budget_view(db, b, today=date(2026, 7, 10))

    assert v.total_ad_spend == Decimal("300.00")          # 100 + 200; Jun 30 & Jul 11 excluded
    assert v.total_promotions == Decimal("500.00")
    assert v.total_committed == Decimal("800.00")
    assert v.available == Decimal("9200.00")
    assert len(v.rows) == 10                              # Jul 1..Jul 10 (clamped to today)
    by_day = {r.day: r for r in v.rows}
    assert by_day[date(2026, 7, 1)].available == Decimal("9900.00")   # −100
    assert by_day[date(2026, 7, 3)].available == Decimal("9400.00")   # promo steps in (−500)
    assert by_day[date(2026, 7, 5)].available == Decimal("9200.00")   # −200 more
    assert v.days_elapsed == 10 and v.days_total == 31
    assert v.is_over_budget is False and v.not_started is False


def test_not_started_budget():
    with SessionLocal() as db:
        b = _budget(db, date(2026, 7, 1), date(2026, 7, 31), 5000)
        db.add(AdBudgetPromotion(ad_budget_id=b.id, name="Reserve", amount=Decimal("1000"),
                                 promo_date=date(2026, 7, 15)))
        db.commit()
        b = db.get(AdBudget, b.id)
        v = compute_budget_view(db, b, today=date(2026, 6, 18))   # before start
    assert v.not_started is True
    assert v.rows == []
    assert v.total_ad_spend == Decimal("0")
    assert v.total_promotions == Decimal("1000.00")
    assert v.available == Decimal("4000.00")               # full budget − promotions
    assert v.days_elapsed == 0


def test_over_budget_flag():
    with SessionLocal() as db:
        bch = _batch(db)
        b = _budget(db, date(2026, 7, 1), date(2026, 7, 31), 100)
        _spend(db, bch.id, date(2026, 7, 2), 150)
        db.commit()
        b = db.get(AdBudget, b.id)
        v = compute_budget_view(db, b, today=date(2026, 7, 3))
    assert v.is_over_budget is True
    assert v.available == Decimal("-50.00")


def test_current_budget_selection():
    with SessionLocal() as db:
        _budget(db, date(2026, 6, 1), date(2026, 6, 30), 1, label="June")
        _budget(db, date(2026, 7, 1), date(2026, 7, 31), 2, label="July")
        db.commit()
        cur = current_budget(db, today=date(2026, 7, 15))
        assert cur is not None and cur.label == "July"
        assert current_budget(db, today=date(2026, 5, 1)) is None   # none covers May
        assert len(list_budgets(db)) == 2


# ---- Routes ----------------------------------------------------------------

def test_create_budget_then_view(client):
    r = client.post("/admin/ad-budget", data={
        "label": "July 2026", "start_date": "2026-07-01",
        "end_date": "2026-07-31", "amount": "10000"}, follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("/admin/ad-budget/")
    page = client.get(loc)
    assert page.status_code == 200
    assert "July 2026" in page.text
    assert "Allocated budget" in page.text


def test_create_budget_validates_dates(client):
    r = client.post("/admin/ad-budget", data={
        "label": "Bad", "start_date": "2026-07-31",
        "end_date": "2026-07-01", "amount": "100"}, follow_redirects=False)
    assert r.status_code == 303
    assert "/admin/ad-budget/new?" in r.headers["location"]
    assert "error" in r.headers["location"]


def test_add_and_delete_promotion(client):
    with SessionLocal() as db:
        b = _budget(db, date(2026, 7, 1), date(2026, 7, 31), 10000); db.commit(); bid = b.id
    # in-range promotion saves
    r = client.post(f"/admin/ad-budget/{bid}/promotions", data={
        "name": "Spring sale", "amount": "500", "promo_date": "2026-07-10"}, follow_redirects=False)
    assert r.status_code == 303 and "Promotion+added" in r.headers["location"]
    with SessionLocal() as db:
        promos = db.query(AdBudgetPromotion).filter_by(ad_budget_id=bid).all()
        assert len(promos) == 1
        pid = promos[0].id
    # out-of-range date rejected
    bad = client.post(f"/admin/ad-budget/{bid}/promotions", data={
        "name": "Late", "amount": "100", "promo_date": "2026-09-01"}, follow_redirects=False)
    assert bad.status_code == 303 and "error" in bad.headers["location"]
    # delete
    d = client.post(f"/admin/ad-budget/{bid}/promotions/{pid}/delete", follow_redirects=False)
    assert d.status_code == 303
    with SessionLocal() as db:
        assert db.query(AdBudgetPromotion).filter_by(ad_budget_id=bid).count() == 0


def test_csv_export(client):
    with SessionLocal() as db:
        bch = _batch(db)
        b = _budget(db, date(2025, 7, 1), date(2025, 7, 31), 10000, label="Jul2025")
        _spend(db, bch.id, date(2025, 7, 1), 100)
        db.commit(); bid = b.id
    r = client.get(f"/admin/ad-budget/{bid}.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "Date,Ad Spend,Promotions,Committed to date,Available remaining" in r.text
    assert "2025-07-01" in r.text


def test_list_renders_empty_state(client):
    r = client.get("/admin/ad-budget")
    assert r.status_code == 200
    assert "No budgets yet" in r.text
