# tests/test_sales_report_email.py
"""Sales report email: render (HTML matches CSV), CSV builder, and send via a
fake mailer."""
import itertools
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.order import Order, OrderLine, OrderType
from app.reports.sales_report import compute_sales_report
from app.services import sales_report_email as sre


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine); Base.metadata.create_all(bind=engine); yield


_OID = itertools.count(1)


def _order(db, d, rev, units):
    b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                    original_filename="t", stored_path="t")
    db.add(b); db.flush()
    o = Order(import_batch_id=b.id, tiktok_order_id=f"O{next(_OID)}",
              placed_at=datetime(d.year, d.month, d.day, 12, 0), order_type=OrderType.PAID,
              status="Completed", brand="smashbox", gross_sales=Decimal(str(rev)),
              shipping_revenue=Decimal("0"), seller_funded_outlandish=Decimal("0"),
              seller_funded_smashbox=Decimal("0"), platform_discount_total=Decimal("0"),
              payment_platform_discount=Decimal("0"))
    db.add(o); db.flush()
    db.add(OrderLine(order_id=o.id, sku="S1", quantity=units, gross_sales=Decimal(str(rev))))
    db.flush()


def _view(db):
    return compute_sales_report(db, "daily", start=date(2026, 5, 1), end=date(2026, 5, 31))


def test_render_and_csv_share_rows():
    with SessionLocal() as db:
        _order(db, date(2026, 5, 10), 100, 3)
        _order(db, date(2026, 5, 12), 50, 2); db.commit()
        view = _view(db)
    subject, html, text = sre.render_sales_email(view, window_label="May 2026")
    csv = sre.build_sales_csv(view).decode()
    assert "May 2026" in subject
    # HTML↔CSV parity: every non-zero bucket's revenue appears in BOTH renderings.
    assert "100.00" in html and "100.00" in csv
    assert "50.00" in html and "50.00" in csv
    # CSV header matches the on-screen export columns.
    assert csv.splitlines()[0] == "Period,Start,Revenue,Units,Orders,AOV,In Progress"


def test_send_sales_report_uses_mailer(monkeypatch):
    calls = {}
    def fake_send(subject, body, *, to, html=None, attachments=None):
        calls.update(subject=subject, to=to, html=html, attachments=attachments)
    monkeypatch.setattr(sre.mailer, "send_email", fake_send)
    with SessionLocal() as db:
        _order(db, date(2026, 5, 10), 100, 3); db.commit()
        sre.send_sales_report(db, recipients=["a@x.com"], granularity="daily",
                              start_date="2026-05-01", end_date="2026-05-31",
                              year=None, month=None)
    assert calls["to"] == ["a@x.com"]
    assert calls["html"] and len(calls["attachments"]) == 1
    assert calls["attachments"][0][0].endswith(".csv")
    assert calls["attachments"][0][2] == "csv"


def test_send_requires_recipients():
    with SessionLocal() as db, pytest.raises(ValueError):
        sre.send_sales_report(db, recipients=[], granularity="daily",
                              start_date=None, end_date=None, year=None, month=None)
