"""Supplier-receipt CSV importer for the sample inventory ledger.

Warehouse intake docs land here: we pre-fill `expected_quantity`, the warehouse
fills `received_quantity`. Each row becomes one SampleInventoryMovement IN row,
batched under the upload's ImportBatch so a bad upload can be rolled back as a
unit via the supplier-receipt branch in `app/services/batch_deletion.py`.

File format
-----------
CSV (or XLSX). Columns are case-insensitive and tolerate spaces / underscores.

  sku                 (required)  TikTok SKU ID, SBX-form, or alt SKU
  received_quantity   (required)  positive int — physical count, source of truth
  received_date       (required)  date — YYYY-MM-DD, MM/DD/YYYY, or YYYY/MM/DD
  expected_quantity   (optional)  cross-check; mismatches noted, never block import
  unit_cost           (optional)  Decimal — supplier cost; column on the ledger
                                  exists but is dormant until reporting needs it
  po_number           (optional)  PO/reference, captured into the movement's note

`expected_quantity` is purely a check value: a blank or unreadable expected
NEVER drops a row — `received_quantity` is the source of truth for stock.

Re-upload behaviour: ADDITIVE — same contract as `samples.py`. There is no
natural key on a receipt log (the same SKU can legitimately be received twice
on the same day under different POs). To roll back a bad upload, delete the
ImportBatch; every IN movement carries `import_batch_id`.

Brand resolution
----------------
Brand is not in the CSV. Resolved per row by looking up `Sku.brand` for the
canonical SKU (tries `tiktok_sku_id`, then `sku`, then `tiktok_alt_sku`).
Falls back to "unknown" when no Sku row matches — the same sentinel used by
the catalog importers and `Creator.platform`.
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.importers.base import BaseImporter, ImportResult
from app.models.import_batch import ImportBatch
from app.models.sku import Sku
from app.services.sample_service import record_sample_receipt
from app.services.sku_alias import load_alias_map

# Canonical column name (lowercase, alphanumeric only) -> attribute name.
COL_ALIASES = {
    "sku": "sku",
    "skuid": "sku",
    "tiktokskuid": "sku",
    "receivedquantity": "received_quantity",
    "receivedqty": "received_quantity",
    "received": "received_quantity",
    "qty": "received_quantity",
    "quantity": "received_quantity",
    "expectedquantity": "expected_quantity",
    "expectedqty": "expected_quantity",
    "expected": "expected_quantity",
    "receiveddate": "received_date",
    "receivedat": "received_date",
    "date": "received_date",
    "unitcost": "unit_cost",
    "cost": "unit_cost",
    "ponumber": "po_number",
    "po": "po_number",
    "purchaseorder": "po_number",
}

REQUIRED = {"sku", "received_quantity", "received_date"}


class SupplierReceiptImporter(BaseImporter):
    def run(self, path: Path, db: Session, batch: ImportBatch) -> ImportResult:
        result = ImportResult()

        df = _read(path)
        df = _normalize_columns(df)

        missing = REQUIRED - set(df.columns)
        if missing:
            raise ValueError(
                f"supplier-receipts file missing required columns: {sorted(missing)}. "
                f"Got: {list(df.columns)}"
            )

        alias_map = load_alias_map(db)
        brand_cache: dict[str, str] = {}

        for i, row in df.iterrows():
            row_num = int(i) + 2  # +1 header, +1 1-indexed display
            try:
                fields = _validate_row(row)
            except ValueError as exc:
                result.skip(f"row {row_num}: {exc}")
                continue

            canonical_sku = alias_map.get(fields["sku"], fields["sku"])
            brand = _resolve_brand(db, canonical_sku, brand_cache)

            record_sample_receipt(
                db,
                sku=fields["sku"],
                quantity=fields["quantity"],
                received_at=fields["received_at"],
                brand=brand,
                unit_cost=fields["unit_cost"],
                import_batch_id=batch.id,
                note=fields["note"],
                shop_id=batch.shop_id,
                alias_map=alias_map,
            )
            result.rows_imported += 1

        return result


def _validate_row(row: pd.Series) -> dict:
    """Per-row validation. Returns kwargs for `record_sample_receipt`.

    Raises ValueError ONLY for problems with real fields (sku, received_quantity,
    received_date, present-but-bad unit_cost). Blank or unreadable
    expected_quantity is tolerated — we degrade the cross-check, not the import.
    """
    sku = _str(row.get("sku"))
    if not sku:
        raise ValueError("missing sku")

    received_raw = _str(row.get("received_quantity"))
    if received_raw is None:
        raise ValueError("missing received_quantity")
    try:
        received_qty = int(float(received_raw))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"bad received_quantity {received_raw!r}") from exc
    if received_qty <= 0:
        raise ValueError(f"non-positive received_quantity {received_qty}")

    received_at = _parse_date(row.get("received_date"))
    if received_at is None:
        raise ValueError(f"unparseable received_date: {row.get('received_date')!r}")

    # Optional unit_cost: blank → None silently (dormant column intent). A present
    # but unparseable value (e.g. "TBD") IS a skip — silent coercion would mask
    # a real data error in the warehouse doc.
    unit_cost_raw = _str(row.get("unit_cost"))
    if unit_cost_raw is None:
        unit_cost: Decimal | None = None
    else:
        try:
            unit_cost = Decimal(unit_cost_raw)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"bad unit_cost {unit_cost_raw!r}") from exc

    # expected_quantity is tolerant — degrade the cross-check, not the row.
    expected_raw = _str(row.get("expected_quantity"))
    expected_qty: int | None
    expected_unreadable = False
    if expected_raw is None:
        expected_qty = None
    else:
        try:
            parsed = int(float(expected_raw))
        except (ValueError, TypeError):
            expected_qty = None
            expected_unreadable = True
        else:
            if parsed < 0:
                expected_qty = None
                expected_unreadable = True
            else:
                expected_qty = parsed

    po_number = _str(row.get("po_number"))
    note = _build_note(po_number, expected_qty, received_qty, expected_unreadable)

    return {
        "sku": sku,
        "quantity": received_qty,
        "received_at": received_at,
        "unit_cost": unit_cost,
        "note": note,
    }


def _build_note(
    po_number: str | None,
    expected_qty: int | None,
    received_qty: int,
    expected_unreadable: bool,
) -> str | None:
    """Compose the movement's note from up to two parts joined by `'; '`."""
    parts: list[str] = []
    if po_number:
        parts.append(f"PO {po_number}")
    if expected_unreadable:
        parts.append("expected: unreadable")
    elif expected_qty is not None and expected_qty != received_qty:
        parts.append(f"expected {expected_qty}, received {received_qty}")
    return "; ".join(parts) if parts else None


def _resolve_brand(db: Session, canonical_sku: str, cache: dict[str, str]) -> str:
    """Look up brand via any of Sku's three identifier columns; cache per call."""
    if canonical_sku in cache:
        return cache[canonical_sku]
    brand = db.execute(
        select(Sku.brand).where(
            (Sku.tiktok_sku_id == canonical_sku)
            | (Sku.sku == canonical_sku)
            | (Sku.tiktok_alt_sku == canonical_sku)
        )
    ).scalars().first()
    resolved = brand or "unknown"
    cache[canonical_sku] = resolved
    return resolved


# ---------- file / cell helpers --------------------------------------------


def _read(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in {".xlsx", ".xls"}:
        return pd.read_excel(path, dtype=str)
    return pd.read_csv(path, dtype=str, encoding="utf-8-sig")


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Lowercase columns, strip whitespace/underscores, map via COL_ALIASES."""
    def canon(c: str) -> str:
        key = "".join(ch for ch in str(c).strip().lower() if ch.isalnum())
        return COL_ALIASES.get(key, key)

    df = df.copy()
    df.columns = [canon(c) for c in df.columns]
    return df


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
