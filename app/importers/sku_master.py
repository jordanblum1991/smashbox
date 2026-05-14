"""Master SKU sheet importer.

Real file layout (2026-05-13 capture):
  workbook  : Master-SKU-Sheet.xlsx
  sheet     : "Master SKU List"
  header row: row 0
  ~497 rows on the sheet, but only rows with a populated `TikTok Shop SKU`
  are real SKUs.

Upsert key
----------
Primary upsert key is `TikTok SKU ID` (the canonical product identifier on
TikTok). One TikTok Shop SKU (SBX-form) may appear on multiple rows, one per
TikTok variation — using SBX-form as the upsert key would silently overwrite
all but the last row. When `TikTok SKU ID` is blank (SKUs not yet listed on
TikTok), we fall back to upserting by `TikTok Shop SKU` so we still capture
COGS/MSRP for inactive items.

Skips rows with no `TikTok Shop SKU`. Never deletes — history matters; an
inactive flag is preserved.
"""
from decimal import Decimal
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.importers.base import BaseImporter, ImportResult
from app.models.import_batch import ImportBatch
from app.models.sku import Sku

SHEET = "Master SKU List"

COL = {
    "name": "Product Name",
    "tiktok_shop_sku": "TikTok Shop SKU",      # canonical SBX-form
    "tiktok_alt_sku": "TikTok ALT SKU",
    "tiktok_sku_id": "TikTok SKU ID",
    "internal_sku": "Internal/SAP SKU",        # informational
    "brand": "Brand",
    "category": "Category",
    "item_type": "Item Type",
    "active_status": "Active Status",
    "msrp": "MSRP",
    "cogs": "Cost / COGS",
}


class SkuMasterImporter(BaseImporter):
    def run(self, path: Path, db: Session, batch: ImportBatch) -> ImportResult:
        result = ImportResult()

        df = pd.read_excel(path, sheet_name=SHEET, dtype=str)
        missing = [c for c in (COL["tiktok_shop_sku"], COL["name"]) if c not in df.columns]
        if missing:
            raise ValueError(f"Master SKU sheet missing required columns: {missing}")

        for _, row in df.iterrows():
            canonical = _str(row.get(COL["tiktok_shop_sku"]))
            if not canonical:
                continue  # scratch row — no SKU populated

            try:
                _upsert_sku(db, canonical, row)
                result.rows_imported += 1
            except Exception as exc:  # noqa: BLE001
                result.skip(f"SKU {canonical}: {exc}")

        return result


def _upsert_sku(db: Session, canonical: str, row: pd.Series) -> None:
    tiktok_sku_id = _str(row.get(COL["tiktok_sku_id"]))

    payload = dict(
        sku=canonical,
        tiktok_alt_sku=_str(row.get(COL["tiktok_alt_sku"])),
        tiktok_sku_id=tiktok_sku_id,
        name=_str(row.get(COL["name"])) or canonical,
        brand=_str(row.get(COL["brand"])) or "unknown",
        category=_str(row.get(COL["category"])),
        item_type=_str(row.get(COL["item_type"])),
        msrp=_dec(row.get(COL["msrp"])),
        unit_cogs=_dec(row.get(COL["cogs"])),
        is_active=(_str(row.get(COL["active_status"])) or "").strip().lower()
        not in {"no", "inactive", "discontinued"},
    )

    # Prefer to upsert by TikTok SKU ID — that's the canonical product
    # identifier and what `sku` (SBX-form) maps to in one-to-many fashion.
    existing = None
    if tiktok_sku_id:
        existing = db.execute(
            select(Sku).where(Sku.tiktok_sku_id == tiktok_sku_id)
        ).scalar_one_or_none()

    # Fallback: SKUs not yet listed on TikTok have no tiktok_sku_id. Upsert by
    # SBX-form so we still capture COGS/MSRP — but ONLY into rows that also
    # have no tiktok_sku_id (otherwise we'd overwrite a sibling variation).
    if existing is None and not tiktok_sku_id:
        existing = db.execute(
            select(Sku)
            .where(Sku.sku == canonical)
            .where(Sku.tiktok_sku_id.is_(None))
        ).scalar_one_or_none()

    if existing is None:
        db.add(Sku(**payload))
    else:
        for k, v in payload.items():
            setattr(existing, k, v)


def _str(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return None
    return s


def _dec(v) -> Decimal:
    s = _str(v)
    if s is None:
        return Decimal("0")
    try:
        return Decimal(s)
    except Exception:  # noqa: BLE001
        return Decimal("0")
