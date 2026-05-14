"""Bundle mapping importer.

Real file layout (2026-05-13 capture):
  workbook  : bundle-mapping.xlsx
  sheet     : "Bundle Mapping"
  header row: row 0
  ~499 rows on the sheet, but only the ones with a populated TIKTOK SKU ID
  are real bundles (~21 today). Components are stored as wide columns
  (Component 1 SKU, Component 1 Name, Component 1 Qty, ... up to 4).

Behaviour:
  - Upsert by `TIKTOK SKU ID` (the bundle's TikTok ID, which is what shows up
    in the orders/settlement files).
  - Replace components on each upsert — single source of truth is the sheet.
"""
from decimal import Decimal
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.importers.base import BaseImporter, ImportResult
from app.models.bundle import Bundle, BundleComponent
from app.models.import_batch import ImportBatch

SHEET = "Bundle Mapping"
MAX_COMPONENTS = 4

COL = {
    "bundle_name": "Bundle Name",
    "bundle_variation": "Bundle Variation ",  # NB: trailing space in real file
    "tiktok_sku_id": "TIKTOK SKU ID",
    "brand": "Brand",
    "active_status": "Active Status",
    "msrp": "Bundle MSRP Value",
    "selling_price": "Bundle Selling Price",
}

# Components 1..N share the same column-name pattern.
def _component_cols(n: int) -> dict[str, str]:
    return {
        "sku": f"Component {n} SKU",
        "name": f"Component {n} Name",
        "qty": f"Component {n} Qty",
        "msrp": f"Component {n} MSRP",
        "cogs": f"Component {n} COGS",
    }


class BundleMappingImporter(BaseImporter):
    def run(self, path: Path, db: Session, batch: ImportBatch) -> ImportResult:
        result = ImportResult()

        df = pd.read_excel(path, sheet_name=SHEET, dtype=str)
        missing = [c for c in (COL["tiktok_sku_id"], COL["bundle_name"]) if c not in df.columns]
        if missing:
            raise ValueError(f"Bundle Mapping sheet missing required columns: {missing}")

        for _, row in df.iterrows():
            tiktok_id = _str(row.get(COL["tiktok_sku_id"]))
            if not tiktok_id:
                continue  # scratch row

            try:
                _upsert_bundle(db, tiktok_id, row)
                result.rows_imported += 1
            except Exception as exc:  # noqa: BLE001
                result.skip(f"bundle {tiktok_id}: {exc}")

        return result


def _upsert_bundle(db: Session, tiktok_id: str, row: pd.Series) -> None:
    existing = db.execute(
        select(Bundle).where(Bundle.tiktok_sku_id == tiktok_id)
    ).scalar_one_or_none()

    components = _read_components(row)
    # Synthesize a bundle_sku from the first component if not given — keeps
    # downstream code that prefers SBX-form keys happy.
    bundle_sku = components[0]["sku"] + "-BUNDLE" if components else None

    payload = dict(
        tiktok_sku_id=tiktok_id,
        bundle_sku=bundle_sku,
        name=_str(row.get(COL["bundle_name"])) or tiktok_id,
        variation=_str(row.get(COL["bundle_variation"])),
        brand=_str(row.get(COL["brand"])) or "unknown",
        is_active=_str(row.get(COL["active_status"])) or "Active",
        msrp=_dec(row.get(COL["msrp"])),
        selling_price=_dec(row.get(COL["selling_price"])),
    )

    if existing is None:
        bundle = Bundle(**payload)
        db.add(bundle)
        db.flush()  # populate bundle.id
    else:
        for k, v in payload.items():
            setattr(existing, k, v)
        # Wipe & rebuild components.
        for c in list(existing.components):
            db.delete(c)
        db.flush()
        bundle = existing

    for c in components:
        db.add(BundleComponent(
            bundle_id=bundle.id,
            component_sku=c["sku"],
            component_name=c["name"],
            quantity=int(c["qty"]) if c["qty"] else 1,
            msrp=_dec(c["msrp"]),
            unit_cogs=_dec(c["cogs"]),
        ))


def _read_components(row: pd.Series) -> list[dict]:
    """Return non-empty component records from columns 1..MAX_COMPONENTS."""
    out: list[dict] = []
    for n in range(1, MAX_COMPONENTS + 1):
        cols = _component_cols(n)
        sku = _str(row.get(cols["sku"]))
        if not sku:
            continue
        out.append({
            "sku": sku,
            "name": _str(row.get(cols["name"])),
            "qty": _str(row.get(cols["qty"])),
            "msrp": row.get(cols["msrp"]),
            "cogs": row.get(cols["cogs"]),
        })
    return out


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
