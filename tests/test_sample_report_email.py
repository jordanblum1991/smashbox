# tests/test_sample_report_email.py
"""Sample report email: render (HTML matches CSV), CSV builder, and send via a
fake mailer. Dataset = samples_by_sku_shipped(db, start, end)."""
import itertools
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.order import Order, OrderLine, OrderType
from app.models.sku import Sku
from app.reports.pnl import PeriodKind, compute_pnl_view, window_for
from app.reports.sample_tracking import samples_by_sku_shipped
from app.services import sample_report_email as sre


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine); Base.metadata.create_all(bind=engine); yield


_OID = itertools.count(1)


def _batch(db):
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                    original_filename="t", stored_path="t")
    db.add(b); db.flush()
    return b


def _sample_order(db, batch, d, sku, qty):
    """A SHIPPED sample order line (counts toward samples_sent)."""
    o = Order(import_batch_id=batch.id, tiktok_order_id=f"O{next(_OID)}",
              placed_at=datetime(d.year, d.month, d.day, 12, 0),
              order_type=OrderType.SAMPLE, status="Shipped", brand="smashbox")
    db.add(o); db.flush()
    db.add(OrderLine(order_id=o.id, sku=sku, quantity=qty, gross_sales=Decimal("0")))
    db.flush()


def _paid_order(db, batch, d, sku, qty, rev):
    o = Order(import_batch_id=batch.id, tiktok_order_id=f"O{next(_OID)}",
              placed_at=datetime(d.year, d.month, d.day, 12, 0),
              order_type=OrderType.PAID, status="Completed", brand="smashbox",
              gross_sales=Decimal(str(rev)))
    db.add(o); db.flush()
    db.add(OrderLine(order_id=o.id, sku=sku, quantity=qty, gross_sales=Decimal(str(rev))))
    db.flush()


def _seed(db):
    db.add(Sku(sku="SBX-001", name="Product One", brand="smashbox",
               tiktok_sku_id="TT-1", unit_cogs=Decimal("5.00")))
    db.flush()
    b = _batch(db)
    _sample_order(db, b, date(2026, 5, 10), "TT-1", 4)
    _paid_order(db, b, date(2026, 5, 12), "TT-1", 8, 200)


def _rows(db):
    view = compute_pnl_view(db, PeriodKind.MONTH, 2026, 5)
    start, end = window_for(view)
    return samples_by_sku_shipped(db, start, end)


def test_render_and_csv_share_rows():
    with SessionLocal() as db:
        _seed(db); db.commit()
        rows = _rows(db)
    assert rows, "expected >=1 shipped-sample row"
    subject, html, text = sre.render_sample_email(rows, title_suffix="May 2026")
    csv = sre.build_sample_csv(rows).decode()
    assert "May 2026" in subject
    # HTML<->CSV parity: the SKU code + the per-row figures appear in BOTH.
    assert "SBX-001" in html and "SBX-001" in csv
    assert "Product One" in html and "Product One" in csv
    # HTML totals row is summed from the SAME rows the CSV is built from — the
    # load-bearing "HTML matches CSV": the summed totals render in the HTML.
    tot_samples = sum(r.samples_sent for r in rows)
    tot_units = sum(r.units_sold for r in rows)
    assert (tot_samples, tot_units) == (4, 8)              # fixture sanity
    assert f">{tot_samples}<" in html and f">{tot_units}<" in html
    # CSV header matches the on-screen export columns.
    assert csv.splitlines()[0] == ("sku_code,name,tiktok_sku_id,samples_sent,"
                                   "sample_orders_shipped,units_sold,sold_per_sample")
    # A known data row is present in the CSV.
    assert "SBX-001,Product One,TT-1,4,1,8" in csv


def test_send_sample_report_uses_mailer(monkeypatch):
    calls = {}
    def fake_send(subject, body, *, to, html=None, attachments=None):
        calls.update(subject=subject, to=to, html=html, attachments=attachments)
    monkeypatch.setattr(sre.mailer, "send_email", fake_send)
    with SessionLocal() as db:
        _seed(db); db.commit()
        sre.send_sample_report(db, recipients=["a@x.com"], period=PeriodKind.MONTH,
                               year=2026, month=5, start_year=None, start_month=None,
                               end_year=None, end_month=None)
    assert calls["to"] == ["a@x.com"]
    assert calls["html"] and len(calls["attachments"]) == 1
    assert calls["attachments"][0][0].endswith(".csv")
    assert calls["attachments"][0][2] == "csv"


def test_send_requires_recipients():
    with SessionLocal() as db, pytest.raises(ValueError):
        sre.send_sample_report(db, recipients=[], period=PeriodKind.MONTH,
                               year=2026, month=5, start_year=None, start_month=None,
                               end_year=None, end_month=None)
