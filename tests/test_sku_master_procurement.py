"""SKU master importer — procurement columns are optional, additive, non-destructive.

Two-shot pattern: upload sheet WITH procurement columns, then upload a sheet
WITHOUT them — the existing per-SKU procurement values must survive the second
upload (operator intent: an absent column means "don't touch", not "blank out").
"""
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from app.db import Base, SessionLocal, engine
from app.importers.sku_master import SHEET, SkuMasterImporter
from app.models import ImportBatch, ImportBatchStatus, ImportFileKind, Sku


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _write_master(path: Path, df: pd.DataFrame) -> None:
    with pd.ExcelWriter(path, engine="xlsxwriter") as xw:
        df.to_excel(xw, sheet_name=SHEET, index=False)


def _run(path: Path):
    with SessionLocal() as db:
        b = ImportBatch(
            kind=ImportFileKind.SKU_MASTER,
            status=ImportBatchStatus.PROCESSING,
            original_filename=path.name,
            stored_path=str(path),
        )
        db.add(b)
        db.flush()
        result = SkuMasterImporter().run(path, db, b)
        db.commit()
        return result


BASE_COLUMNS = [
    "Product Name", "TikTok Shop SKU", "TikTok ALT SKU", "TikTok SKU ID",
    "Brand", "Category", "Item Type", "Active Status", "MSRP", "Cost / COGS",
]


def test_procurement_columns_populated_when_present(tmp_path):
    path = tmp_path / "master_with_procurement.xlsx"
    df = pd.DataFrame([{
        "Product Name": "Widget",
        "TikTok Shop SKU": "SBX-W1",
        "TikTok ALT SKU": "W1",
        "TikTok SKU ID": "1000000000001",
        "Brand": "smashbox",
        "Category": "Tools",
        "Item Type": "Single",
        "Active Status": "Active",
        "MSRP": "29.99",
        "Cost / COGS": "8.50",
        "Lead Time Days": "21",
        "MOQ": "100",
        "Case Pack": "12",
        "Safety Stock %": "15",
        "Reorderable": "yes",
    }])
    _write_master(path, df)
    result = _run(path)
    assert result.rows_imported == 1

    with SessionLocal() as db:
        sku = db.query(Sku).one()
        assert sku.lead_time_days == 21
        assert sku.moq == 100
        assert sku.case_pack == 12
        assert sku.safety_stock_pct == Decimal("15")
        assert sku.is_reorderable is True


def test_absent_procurement_columns_preserve_existing_values(tmp_path):
    """First upload populates procurement; second upload omits the columns
    entirely. Procurement values must persist."""
    # Shot 1 — with procurement columns
    path1 = tmp_path / "with_proc.xlsx"
    df1 = pd.DataFrame([{
        "Product Name": "Widget", "TikTok Shop SKU": "SBX-W1",
        "TikTok ALT SKU": "W1", "TikTok SKU ID": "1000000000001",
        "Brand": "smashbox", "Category": "Tools", "Item Type": "Single",
        "Active Status": "Active", "MSRP": "29.99", "Cost / COGS": "8.50",
        "Lead Time Days": "21", "MOQ": "100", "Case Pack": "12",
        "Safety Stock %": "15", "Reorderable": "yes",
    }])
    _write_master(path1, df1)
    _run(path1)

    # Shot 2 — base columns only, no procurement
    path2 = tmp_path / "without_proc.xlsx"
    df2 = pd.DataFrame([{c: ("Widget v2" if c == "Product Name" else
                              "SBX-W1" if c == "TikTok Shop SKU" else
                              "W1" if c == "TikTok ALT SKU" else
                              "1000000000001" if c == "TikTok SKU ID" else
                              "smashbox" if c == "Brand" else
                              "Active" if c == "Active Status" else
                              "29.99" if c == "MSRP" else
                              "8.50" if c == "Cost / COGS" else "")
                          for c in BASE_COLUMNS}])
    _write_master(path2, df2)
    _run(path2)

    with SessionLocal() as db:
        sku = db.query(Sku).one()
        assert sku.name == "Widget v2"
        # Procurement values survive untouched
        assert sku.lead_time_days == 21
        assert sku.moq == 100
        assert sku.case_pack == 12
        assert sku.safety_stock_pct == Decimal("15")
        assert sku.is_reorderable is True


def test_blank_procurement_cell_with_column_present_clears_value(tmp_path):
    """If the column is in the sheet but a cell is blank, the operator is
    saying 'unset this' — write null."""
    path1 = tmp_path / "set.xlsx"
    df1 = pd.DataFrame([{
        "Product Name": "Widget", "TikTok Shop SKU": "SBX-W1",
        "TikTok ALT SKU": "", "TikTok SKU ID": "1000000000001",
        "Brand": "smashbox", "Category": "", "Item Type": "",
        "Active Status": "Active", "MSRP": "29.99", "Cost / COGS": "8.50",
        "Lead Time Days": "21", "MOQ": "100", "Case Pack": "12",
        "Safety Stock %": "15", "Reorderable": "yes",
    }])
    _write_master(path1, df1)
    _run(path1)

    path2 = tmp_path / "blank.xlsx"
    df2 = pd.DataFrame([{
        "Product Name": "Widget", "TikTok Shop SKU": "SBX-W1",
        "TikTok ALT SKU": "", "TikTok SKU ID": "1000000000001",
        "Brand": "smashbox", "Category": "", "Item Type": "",
        "Active Status": "Active", "MSRP": "29.99", "Cost / COGS": "8.50",
        "Lead Time Days": "", "MOQ": "", "Case Pack": "",
        "Safety Stock %": "", "Reorderable": "",
    }])
    _write_master(path2, df2)
    _run(path2)

    with SessionLocal() as db:
        sku = db.query(Sku).one()
        assert sku.lead_time_days is None
        assert sku.moq is None
        assert sku.case_pack is None
        assert sku.safety_stock_pct is None
        # is_reorderable defaults True when the cell is blank-but-present
        assert sku.is_reorderable is True


def test_is_reorderable_false_values_recognized(tmp_path):
    path = tmp_path / "discontinued.xlsx"
    df = pd.DataFrame([{
        "Product Name": "Old SKU", "TikTok Shop SKU": "SBX-OLD",
        "TikTok ALT SKU": "", "TikTok SKU ID": "1000000000999",
        "Brand": "smashbox", "Category": "", "Item Type": "",
        "Active Status": "Active", "MSRP": "0", "Cost / COGS": "0",
        "Lead Time Days": "0", "MOQ": "0", "Case Pack": "0",
        "Safety Stock %": "0", "Reorderable": "no",
    }])
    _write_master(path, df)
    _run(path)

    with SessionLocal() as db:
        sku = db.query(Sku).one()
        assert sku.is_reorderable is False
