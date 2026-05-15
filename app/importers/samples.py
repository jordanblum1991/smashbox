"""Off-platform samples importer.

The `samples` table is for sample units that did NOT ship through TikTok Shop
(creator seeding via Shopify, direct ship, agency drops, etc.). TikTok-Shop
samples are detected from the orders file (gross_sales == 0, or settlement
file's "free sample" flag — see tiktok_orders.py and tiktok_settlements.py).
The sample-tracking report unions both sources.

File format
-----------
CSV (or XLSX) with these columns. Column names are case-insensitive and tolerate
underscores or spaces.

  shipped_at          (required)  date — YYYY-MM-DD, MM/DD/YYYY, or YYYY/MM/DD
  sku                 (required)  TikTok SKU ID, SBX-form, or alt SKU — the
                                  sample-tracking report resolves any of the
                                  three against the catalog.
  quantity            (optional)  positive int; defaults to 1
  creator_handle      (optional)  @-handle or freeform name
  is_paid_oversample  (optional)  true / false / yes / no / 1 / 0
  note                (optional)  freeform

A downloadable template lives at `/static/templates/samples_template.csv`.

Re-upload behaviour: ADDITIVE — each upload creates new rows. There is no
natural key on a manual log (the same SKU can legitimately ship to the same
creator twice on the same day). To roll back a bad upload, delete the
ImportBatch row; every Sample carries `import_batch_id`.
"""
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

from app.importers.base import BaseImporter, ImportResult
from app.models.import_batch import ImportBatch
from app.models.sample import Sample

# Canonical column name (lowercase, no spaces/underscores) -> attribute name.
COL_ALIASES = {
    "shippedat": "shipped_at",
    "shipdate": "shipped_at",
    "date": "shipped_at",
    "sku": "sku",
    "skuid": "sku",
    "tiktokskuid": "sku",
    "quantity": "quantity",
    "qty": "quantity",
    "units": "quantity",
    "creatorhandle": "creator_handle",
    "creator": "creator_handle",
    "handle": "creator_handle",
    "ispaidoversample": "is_paid_oversample",
    "paidoversample": "is_paid_oversample",
    "paid": "is_paid_oversample",
    "note": "note",
    "notes": "note",
}

REQUIRED = {"shipped_at", "sku"}

TRUE_VALUES = {"true", "t", "yes", "y", "1"}


class SamplesImporter(BaseImporter):
    def run(self, path: Path, db: Session, batch: ImportBatch) -> ImportResult:
        result = ImportResult()

        df = _read(path)
        df = _normalize_columns(df)

        missing = REQUIRED - set(df.columns)
        if missing:
            raise ValueError(
                f"samples file missing required columns: {sorted(missing)}. "
                f"Got: {list(df.columns)}"
            )

        for i, row in df.iterrows():
            row_num = int(i) + 2  # +1 for header, +1 for 1-indexed display
            try:
                sample = _build_sample(row, batch)
            except ValueError as exc:
                result.skip(f"row {row_num}: {exc}")
                continue
            db.add(sample)
            result.rows_imported += 1

        return result


def _read(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in {".xlsx", ".xls"}:
        return pd.read_excel(path, dtype=str)
    # CSV. utf-8-sig handles the BOM Excel adds when saving as CSV.
    return pd.read_csv(path, dtype=str, encoding="utf-8-sig")


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Lowercase columns, strip whitespace/underscores, map via COL_ALIASES."""
    def canon(c: str) -> str:
        key = "".join(ch for ch in str(c).strip().lower() if ch.isalnum())
        return COL_ALIASES.get(key, key)

    df = df.copy()
    df.columns = [canon(c) for c in df.columns]
    return df


def _build_sample(row: pd.Series, batch: ImportBatch) -> Sample:
    shipped = _parse_date(row.get("shipped_at"))
    if shipped is None:
        raise ValueError(f"unparseable shipped_at: {row.get('shipped_at')!r}")

    sku = _str(row.get("sku"))
    if not sku:
        raise ValueError("missing sku")

    qty_raw = _str(row.get("quantity"))
    try:
        qty = int(float(qty_raw)) if qty_raw else 1
    except ValueError as exc:
        raise ValueError(f"bad quantity {qty_raw!r}") from exc
    if qty <= 0:
        raise ValueError(f"non-positive quantity {qty}")

    is_paid = (_str(row.get("is_paid_oversample")) or "").lower() in TRUE_VALUES

    return Sample(
        import_batch_id=batch.id,
        shipped_at=shipped,
        sku=sku,
        quantity=qty,
        creator_handle=_str(row.get("creator_handle")),
        is_paid_oversample=is_paid,
        note=_str(row.get("note")),
    )


# ---------- helpers ---------------------------------------------------------

def _str(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return None
    return s


def _parse_date(v) -> datetime | None:
    s = _str(v)
    if s is None:
        return None
    s = s.split(" ")[0]  # drop time component if any
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None
