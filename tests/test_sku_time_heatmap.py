# tests/test_sku_time_heatmap.py
"""SKU × time heatmap: PAID units bucketed by shop-local weekday/daypart, per-row
leveling, top-N ranking, insights. Buckets derived via placed_local() (DST-robust)."""
import itertools
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.order import Order, OrderLine, OrderType
from app.models.sku import Sku
from app.reports.sku_time_heatmap import compute_sku_time_heatmap
from app.services.reporting_tz import placed_local

_OID = itertools.count(1)
WSTART, WEND = date(2026, 5, 1), date(2026, 5, 31)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _sku(db, tid, code, name):
    db.add(Sku(sku=code, name=name, brand="smashbox", tiktok_sku_id=tid, unit_cogs=Decimal("0")))
    db.flush()


def _order(db, dt, sku_id, qty, order_type=OrderType.PAID):
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                    original_filename="t", stored_path="t")
    db.add(b); db.flush()
    o = Order(import_batch_id=b.id, tiktok_order_id=f"O{next(_OID)}", placed_at=dt,
              order_type=order_type, status="Completed", brand="smashbox",
              gross_sales=Decimal(str(qty * 10)))
    db.add(o); db.flush()
    db.add(OrderLine(order_id=o.id, sku=sku_id, quantity=qty, gross_sales=Decimal(str(qty * 10))))
    db.flush()


def test_units_bucket_to_weekday():
    dt = datetime(2026, 5, 20, 14, 0)
    with SessionLocal() as db:
        _sku(db, "S1", "SBX-1", "Primer")
        _order(db, dt, "S1", 7); db.commit()
        v = compute_sku_time_heatmap(db, start=WSTART, end=WEND, dim="dow")
    assert v.columns == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    row = next(r for r in v.rows if r.sku_id == "S1")
    wd = placed_local(dt).weekday()
    assert row.cells[wd].units == 7
    assert row.cells[wd].level == 4          # its own peak
    assert row.peak_label == v.columns[wd]
    assert all(c.units == 0 and c.level == 0 for c in row.cells if c.bucket != wd)


def test_daypart_dim():
    dt = datetime(2026, 5, 20, 14, 0)
    with SessionLocal() as db:
        _sku(db, "S1", "SBX-1", "Primer")
        _order(db, dt, "S1", 5); db.commit()
        v = compute_sku_time_heatmap(db, start=WSTART, end=WEND, dim="daypart")
    assert v.columns == ["Morning", "Afternoon", "Evening", "Night"]
    h = placed_local(dt).hour
    exp = (0 if 5 <= h < 12 else 1 if 12 <= h < 17 else 2 if 17 <= h < 22 else 3)
    row = v.rows[0]
    assert row.cells[exp].units == 5 and row.cells[exp].level == 4


def test_per_row_leveling_is_relative():
    big_day, small_day = datetime(2026, 5, 20, 12, 0), datetime(2026, 5, 22, 12, 0)
    with SessionLocal() as db:
        _sku(db, "BIG", "SBX-B", "Big"); _sku(db, "SMALL", "SBX-S", "Small")
        _order(db, big_day, "BIG", 100); _order(db, small_day, "BIG", 1)
        _order(db, big_day, "SMALL", 2)            # low volume overall
        db.commit()
        v = compute_sku_time_heatmap(db, start=WSTART, end=WEND, dim="dow")
    by = {r.sku_id: r for r in v.rows}
    wd_big, wd_small = placed_local(big_day).weekday(), placed_local(small_day).weekday()
    assert by["BIG"].cells[wd_big].level == 4          # peak
    assert by["BIG"].cells[wd_small].level >= 1        # non-zero → at least level 1
    assert by["BIG"].cells[wd_small].level < 4
    # Per-row scaling: the low-volume SKU still hits level 4 in its own peak bucket.
    assert by["SMALL"].cells[wd_big].level == 4


def test_top_n_ranking():
    with SessionLocal() as db:
        for i in range(1, 26):                          # 25 SKUs, SKU i has i units
            _sku(db, f"T{i:02d}", f"SBX-{i:02d}", f"P{i}")
            _order(db, datetime(2026, 5, 20, 12, 0), f"T{i:02d}", i)
        db.commit()
        v = compute_sku_time_heatmap(db, start=WSTART, end=WEND, dim="dow", top_n=20)
    assert v.total_skus == 25
    assert v.shown == 20
    assert v.rows[0].sku_id == "T25" and v.rows[0].total_units == 25   # ranked desc
    assert all(r.sku_id != "T01" for r in v.rows)        # the 5 smallest dropped


def test_busiest_col_unmapped_paid_only_and_empty():
    with SessionLocal() as db:
        # Unmapped SKU "RAW" (no Sku row); a SAMPLE order must be excluded.
        _order(db, datetime(2026, 5, 20, 12, 0), "RAW", 4)
        _order(db, datetime(2026, 5, 21, 12, 0), "RAW", 99, order_type=OrderType.SAMPLE)
        db.commit()
        v = compute_sku_time_heatmap(db, start=WSTART, end=WEND, dim="dow")
    raw = next(r for r in v.rows if r.sku_id == "RAW")
    assert raw.code == "Unmapped"
    assert raw.total_units == 4                          # SAMPLE excluded
    assert v.busiest_col == v.columns[placed_local(datetime(2026, 5, 20, 12, 0)).weekday()]

    Base.metadata.drop_all(bind=engine); Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        v2 = compute_sku_time_heatmap(db, start=WSTART, end=WEND, dim="dow")
    assert v2.rows == [] and v2.total_skus == 0 and v2.busiest_col is None


def test_invalid_dim_falls_back_to_dow():
    with SessionLocal() as db:
        _order(db, datetime(2026, 5, 20, 12, 0), "RAW", 1); db.commit()
        v = compute_sku_time_heatmap(db, start=WSTART, end=WEND, dim="bogus")
    assert v.dim == "dow" and len(v.columns) == 7
