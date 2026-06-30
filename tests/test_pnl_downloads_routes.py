"""The P&L statement downloads page + the per-fiscal-month CSV / PDF exports."""
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.import_batch import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.order import Order, OrderType
from app.models.shop import Shop


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        db.add(Shop(slug="smashbox", name="Smashbox", timezone="America/Los_Angeles"))
        b = ImportBatch(kind=ImportFileKind.TIKTOK_ORDERS, status=ImportBatchStatus.COMPLETED,
                        original_filename="t", stored_path="t")
        db.add(b); db.flush()
        # A PAID order inside fiscal May 2026 (Apr 29 – May 28).
        db.add(Order(import_batch_id=b.id, tiktok_order_id="O1", order_type=OrderType.PAID,
                     status="Shipped", brand="smashbox",
                     placed_at=datetime(2026, 5, 15, 12, 0), gross_sales=Decimal("100")))
        db.commit()
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_downloads_page_lists_fiscal_month_with_csv_and_pdf_links(client):
    r = client.get("/reports/pnl/downloads")
    assert r.status_code == 200
    assert "May 2026" in r.text
    assert "Fiscal May 2026" not in r.text       # the "Fiscal" prefix is dropped here
    assert "/export/pnl.csv?period=fiscal_month&year=2026&month=5" in r.text
    assert "/export/pnl.pdf?period=fiscal_month&year=2026&month=5" in r.text


def test_pnl_csv_export(client):
    r = client.get("/export/pnl.csv?period=fiscal_month&year=2026&month=5")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert 'smashbox_pnl_fiscal_2026-05.csv' in r.headers["content-disposition"]
    body = r.text
    assert "Fiscal May 2026" in body
    assert "Gross Product Sales" in body
    assert "Net Profit" in body


def test_pnl_pdf_template_renders_to_html():
    # Exercises the PDF Jinja template + statement_lines without WeasyPrint, so
    # template errors are caught even where the PDF system libs aren't installed.
    from app.reports.pnl import PeriodKind, compute_pnl_view
    from app.reports.pnl_statement import statement_lines
    from app.templating import templates
    with SessionLocal() as db:
        view = compute_pnl_view(db, PeriodKind.FISCAL_MONTH, 2026, 5)
    html = templates.get_template("reports/pnl_pdf.html").render(
        {"view": view, "lines": statement_lines(view.total), "request": None}
    )
    assert "Smashbox P&amp;L" in html
    assert "Net Profit" in html
    assert "Net Margin" in html


def test_pnl_pdf_export(client):
    try:
        import weasyprint  # noqa: F401
    except Exception:
        pytest.skip("WeasyPrint system libs not installed in this environment")
    r = client.get("/export/pnl.pdf?period=fiscal_month&year=2026&month=5")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert 'smashbox_pnl_fiscal_2026-05.pdf' in r.headers["content-disposition"]
    assert r.content[:4] == b"%PDF"
