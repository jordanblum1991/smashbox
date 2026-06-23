"""Inventory report email: HTML/text rendering, formatted xlsx, and the send
orchestration (SMTP seam monkeypatched)."""
import io
from datetime import datetime
from decimal import Decimal

import openpyxl

from app.reports.inventory_report import InventoryReportRow, InventoryReportView
import app.services.inventory_report_email as ire


def _view(last_sync=datetime(2026, 6, 23, 7, 30)):
    row = InventoryReportRow(
        canonical_sku="111", sku_code="SBX-OG-PRIMER", name="OG Primer",
        is_bundle=False, sellable_on_hand=640, sample_on_hand=8, total_on_hand=648,
        in_transit=48, unit_cogs=Decimal("3.00"),
        sellable_value=Decimal("1920.00"), sample_value=Decimal("24.00"),
        total_value=Decimal("1944.00"))
    return InventoryReportView(
        rows=[row], total_sellable=640, total_sample=8, total_units=648,
        total_in_transit=48, sku_count=1,
        total_sellable_value=Decimal("1920.00"), total_sample_value=Decimal("24.00"),
        total_inventory_value=Decimal("1944.00"),
        last_synced_at=last_sync, as_of=datetime(2026, 6, 23, 9, 0))


def test_render_html_has_row_totals_and_snapshot():
    subject, html, text = ire.render_inventory_email(_view())
    assert "Inventory" in subject and "Jun 23, 2026" in subject
    assert "SBX-OG-PRIMER" in html and "OG Primer" in html
    assert "640" in html and "48" in html
    assert "648" in html  # total units
    assert "2026-06-23" in html and "2026-06-23" in text
    assert "style=" in html and "class=" not in html


def test_render_handles_no_snapshot():
    subject, html, text = ire.render_inventory_email(_view(last_sync=None))
    assert "no snapshot" in html.lower()
    assert "no snapshot" in text.lower()


def test_html_escapes_special_chars_in_names():
    v = _view()
    v.rows[0].name = "Smooth & Blur <Primer>"
    subject, html, text = ire.render_inventory_email(v)
    assert "Smooth &amp; Blur &lt;Primer&gt;" in html
    # text body keeps the raw ampersand (plain text, not escaped)
    assert "Smooth & Blur <Primer>" in text


def test_build_xlsx_readback():
    data = ire.build_inventory_xlsx(_view())
    assert data[:4] == b"PK\x03\x04"            # xlsx is a zip
    wb = openpyxl.load_workbook(io.BytesIO(data))
    ws = wb.active
    flat = [v for rowvals in ws.iter_rows(values_only=True) for v in rowvals]
    assert "SBX-OG-PRIMER" in flat
    assert any(isinstance(v, str) and "Inventory as of" in v for v in flat)  # caption
    assert any(isinstance(v, str) and v.upper() == "TOTAL" for v in flat)    # totals row


def test_send_inventory_report_calls_mailer(monkeypatch):
    captured = {}

    def fake_send(subject, body, *, to, html=None, attachments=None):
        captured.update(subject=subject, body=body, to=to, html=html,
                        attachments=attachments)

    monkeypatch.setattr(ire, "compute_inventory_report", lambda db: _view())
    monkeypatch.setattr(ire.mailer, "send_email", fake_send)
    ire.send_inventory_report(db=None, recipients=["a@x.com", "b@x.com"])
    assert captured["to"] == ["a@x.com", "b@x.com"]
    assert "<table" in captured["html"]
    assert captured["attachments"][0][0].endswith(".xlsx")
    assert captured["attachments"][0][2] == "vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_send_inventory_report_rejects_empty_recipients():
    import pytest
    with pytest.raises(ValueError):
        ire.send_inventory_report(db=None, recipients=[])
