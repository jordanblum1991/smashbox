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
    assert "Inventory" in subject
    assert "SBX-OG-PRIMER" in html and "OG Primer" in html
    assert "640" in html and "48" in html
    assert "648" in html  # total units
    assert "2026-06-23" in html and "2026-06-23" in text
    assert "style=" in html and "class=" not in html


def test_render_handles_no_snapshot():
    subject, html, text = ire.render_inventory_email(_view(last_sync=None))
    assert "no snapshot" in html.lower()
    assert "no snapshot" in text.lower()
