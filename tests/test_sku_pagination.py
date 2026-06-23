# tests/test_sku_pagination.py
"""Pagination on the SKUs tab: 10/25/50/100 size selector (default 25) + pager.
Seeds 30 PAID SKUs (SKU i gets i units → deterministic units-desc order:
SBX-030 top … SBX-001 last). Names are distinct from codes so table-name checks
aren't polluted by the insights strip (which renders codes only)."""
import itertools
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.order import Order, OrderLine, OrderType
from app.models.sku import Sku

_OID = itertools.count(1)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _seed_n(db, n):
    """n SKUs; SKU i (1..n) gets i units in one PAID order placed now."""
    now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)
    for i in range(1, n + 1):
        code = f"SBX-{i:03d}"
        db.add(Sku(sku=code, name=f"ProductName{i:03d}", brand="smashbox",
                   tiktok_sku_id=f"T{i:03d}", unit_cogs=Decimal("0")))
        db.flush()
        b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                        original_filename="t", stored_path="t")
        db.add(b); db.flush()
        o = Order(import_batch_id=b.id, tiktok_order_id=f"O{next(_OID)}", placed_at=now,
                  order_type=OrderType.PAID, status="Completed", brand="smashbox",
                  gross_sales=Decimal(str(i * 10)))
        db.add(o); db.flush()
        db.add(OrderLine(order_id=o.id, sku=f"T{i:03d}", quantity=i,
                         gross_sales=Decimal(str(i * 10))))
        db.flush()


# Units-desc: SBX-030 (30u) … SBX-006 is the 25th, SBX-005 the 26th, SBX-001 last.
# Negative assertions avoid SBX-030 (the insights strip always renders the top seller).

def test_default_page_size_25_first_page(client):
    with SessionLocal() as db:
        _seed_n(db, 30); db.commit()
    r = client.get("/reports/sales?tab=skus")
    assert r.status_code == 200
    assert "of 30" in r.text              # total count shown
    assert "SBX-030" in r.text            # top seller on page 1
    assert "SBX-005" not in r.text        # 26th — belongs to page 2
    assert "SBX-001" not in r.text        # last — page 2


def test_second_page_shows_remainder(client):
    with SessionLocal() as db:
        _seed_n(db, 30); db.commit()
    r = client.get("/reports/sales?tab=skus&per_page=25&page=2")
    assert r.status_code == 200
    assert "SBX-001" in r.text            # remainder on page 2
    assert "SBX-006" not in r.text        # 25th — was on page 1


def test_invalid_per_page_falls_back_to_25(client):
    with SessionLocal() as db:
        _seed_n(db, 30); db.commit()
    r = client.get("/reports/sales?tab=skus&per_page=7")
    assert r.status_code == 200
    assert "SBX-006" in r.text            # the 25th row present → size is 25, not 7
    assert "SBX-005" not in r.text


def test_page_out_of_range_clamps_to_last(client):
    with SessionLocal() as db:
        _seed_n(db, 30); db.commit()
    r = client.get("/reports/sales?tab=skus&per_page=25&page=99")
    assert r.status_code == 200
    assert "SBX-001" in r.text            # clamped to the last page (page 2)


def test_per_page_100_shows_all(client):
    with SessionLocal() as db:
        _seed_n(db, 30); db.commit()
    r = client.get("/reports/sales?tab=skus&per_page=100")
    assert r.status_code == 200
    assert "SBX-001" in r.text and "SBX-006" in r.text   # all 30 on one page


def test_overview_unaffected(client):
    r = client.get("/reports/sales")
    assert r.status_code == 200
    assert "Revenue velocity" in r.text
