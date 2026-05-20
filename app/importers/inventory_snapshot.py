"""On-hand inventory snapshot importer (Phase A of demand planning).

File format
-----------
CSV or XLSX with these columns. Column names are case-insensitive and tolerate
underscores or spaces.

  sku           (required)  TikTok SKU ID, SBX-form, or alt SKU — the demand
                            planner resolves any of the three against the
                            catalog at compute time.
  on_hand       (required)  non-negative integer
  captured_at   (optional)  date or datetime — when the count was taken.
                            Defaults to the upload time when missing/blank.

A downloadable template lives at `/static/templates/inventory_snapshot_template.csv`.

Re-upload behaviour: IDEMPOTENT on (sku, captured_at). Re-uploading the same
snapshot updates on_hand in place rather than appending a duplicate row, so an
operator can safely re-send a corrected file without bloating history.

Manual API is intentionally identical to what a future Shopify/3PL API client
would produce — when that ships, the API client builds an in-memory DataFrame
with the same columns and reuses the import path.
"""
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.importers.base import BaseImporter, ImportResult
from app.models.import_batch import ImportBatch, _utc_now_naive
from app.models.inventory_snapshot import InventorySnapshot

# Canonical column name (lowercase, no spaces/underscores) -> attribute name.
COL_ALIASES = {
    "sku": "sku",
    "skuid": "sku",
    "tiktokskuid": "sku",
    "onhand": "on_hand",
    "stock": "on_hand",
    "quantity": "on_hand",
    "qty": "on_hand",
    "units": "on_hand",
    "capturedat": "captured_at",
    "snapshotat": "captured_at",
    "date": "captured_at",
    "asof": "captured_at",
}

REQUIRED = {"sku", "on_hand"}


class InventorySnapshotImporter(BaseImporter):
    def run(self, path: Path, db: Session, batch: ImportBatch) -> ImportResult:
        result = ImportResult()

        df = _read(path)
        df = _normalize_columns(df)

        missing = REQUIRED - set(df.columns)
        if missing:
            raise ValueError(
                f"inventory snapshot file missing required columns: {sorted(missing)}. "
                f"Got: {list(df.columns)}"
            )

        upload_time = _utc_now_naive()

        for i, row in df.iterrows():
            row_num = int(i) + 2  # +1 for header, +1 for 1-indexed display
            try:
                sku, on_hand, captured_at = _parse_row(row, upload_time)
            except ValueError as exc:
                result.skip(f"row {row_num}: {exc}")
                continue

            existing = db.execute(
                select(InventorySnapshot)
                .where(InventorySnapshot.sku == sku)
                .where(InventorySnapshot.captured_at == captured_at)
            ).scalar_one_or_none()

            if existing is None:
                db.add(InventorySnapshot(
                    import_batch_id=batch.id,
                    sku=sku,
                    on_hand=on_hand,
                    captured_at=captured_at,
                ))
            else:
                existing.on_hand = on_hand
                existing.import_batch_id = batch.id
            result.rows_imported += 1

        return result


def _read(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in {".xlsx", ".xls"}:
        return pd.read_excel(path, dtype=str)
    return pd.read_csv(path, dtype=str, encoding="utf-8-sig")


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    def canon(c: str) -> str:
        key = "".join(ch for ch in str(c).strip().lower() if ch.isalnum())
        return COL_ALIASES.get(key, key)

    df = df.copy()
    df.columns = [canon(c) for c in df.columns]
    return df


def _parse_row(row: pd.Series, upload_time: datetime) -> tuple[str, int, datetime]:
    sku = _str(row.get("sku"))
    if not sku:
        raise ValueError("missing sku")

    on_hand_raw = _str(row.get("on_hand"))
    if on_hand_raw is None:
        raise ValueError("missing on_hand")
    try:
        on_hand = int(float(on_hand_raw))
    except ValueError as exc:
        raise ValueError(f"bad on_hand {on_hand_raw!r}") from exc
    if on_hand < 0:
        raise ValueError(f"negative on_hand {on_hand}")

    captured = _parse_date(row.get("captured_at")) or upload_time
    return sku, on_hand, captured


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
    s = s.split(" ")[0]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None
