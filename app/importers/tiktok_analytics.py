"""TikTok Shop Analytics → Key metrics export importer.

Source: Seller Center → Analytics → Sales → Export, filename pattern
`Shop Analytics_Key metrics_*.xlsx`.

File layout (header positions are deliberate — TikTok's export buries them):
  Row 0       : "Analysis date: ..." metadata
  Row 1       : "Data overview" label
  Row 2       : Column headers for the totals section
  Row 3       : "Total value" — period aggregate
  Row 4       : "Percentage change" — comparison-window delta
  Row 5–6     : blank
  Row 7       : "Daily data" label
  Row 8       : Column headers for the per-day rows (we read with header=8)
  Row 9+      : One row per calendar day, Date in DD/MM/YYYY string

Sign / format quirks:
- `Items refunded` and AOV cells render as "-" (em dash or hyphen) when no
  refund/order activity that day. `_int` and `_dec` normalize those to 0.
- Dates are European (DD/MM/YYYY) — explicit `%d/%m/%Y` parse, never let
  pandas auto-detect (it'll silently flip month/day in ambiguous months).
- Re-uploading is idempotent — upsert by metric_date, so TikTok's overnight
  corrections to yesterday's numbers flow through cleanly.
"""
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.importers.base import BaseImporter, ImportResult
from app.models.import_batch import ImportBatch
from app.models.tiktok_daily_metric import TikTokDailyMetric

HEADER_ROW = 8

COL = {
    "date": "Date",
    "gmv": "GMV",
    "orders": "Orders",
    "customers": "Customers",
    "items_sold": "Items sold",
    "items_canceled_returned": "Items canceled and returned",
    "items_refunded": "Items refunded",
    "aov": "AOV",
    "gmv_with_tax": "GMV (with tax)",
    "tax": "Tax",
    "shipping_fees": "Shipping fees",
}


class TikTokAnalyticsImporter(BaseImporter):
    def run(self, path: Path, db: Session, batch: ImportBatch) -> ImportResult:
        result = ImportResult()

        df = pd.read_excel(path, header=HEADER_ROW, dtype=str)
        missing = [c for c in (COL["date"], COL["gmv"]) if c not in df.columns]
        if missing:
            raise ValueError(f"Shop Analytics file missing required columns: {missing}")

        for _, row in df.iterrows():
            raw_date = _str(row.get(COL["date"]))
            if raw_date is None:
                continue
            try:
                day = datetime.strptime(raw_date, "%d/%m/%Y").date()
            except ValueError:
                result.skip(f"unparseable date: {raw_date!r}")
                continue
            try:
                _upsert(db, day, row, batch)
                result.rows_imported += 1
            except Exception as exc:  # noqa: BLE001
                result.skip(f"{day}: {exc}")

        return result


def _upsert(db: Session, day, row: pd.Series, batch: ImportBatch) -> TikTokDailyMetric:
    payload = dict(
        import_batch_id=batch.id,
        metric_date=day,
        gmv=_dec(row.get(COL["gmv"])),
        orders=_int(row.get(COL["orders"])),
        customers=_int(row.get(COL["customers"])),
        items_sold=_int(row.get(COL["items_sold"])),
        items_canceled_returned=_int(row.get(COL["items_canceled_returned"])),
        items_refunded=_int(row.get(COL["items_refunded"])),
        aov=_dec(row.get(COL["aov"])),
        gmv_with_tax=_dec(row.get(COL["gmv_with_tax"])),
        tax=_dec(row.get(COL["tax"])),
        shipping_fees=_dec(row.get(COL["shipping_fees"])),
    )

    existing = db.execute(
        select(TikTokDailyMetric).where(TikTokDailyMetric.metric_date == day)
    ).scalar_one_or_none()
    if existing is None:
        obj = TikTokDailyMetric(**payload)
        db.add(obj)
        return obj
    for k, v in payload.items():
        setattr(existing, k, v)
    return existing


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


def _dec(v) -> Decimal:
    """Parse a money/numeric cell. TikTok renders empty values as "-" (and
    occasionally em-dash); both normalize to 0."""
    s = _str(v)
    if s is None or s in {"-", "—", "–"}:
        return Decimal("0")
    try:
        return Decimal(s.replace(",", ""))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def _int(v) -> int:
    s = _str(v)
    if s is None or s in {"-", "—", "–"}:
        return 0
    try:
        return int(Decimal(s.replace(",", "")))
    except Exception:  # noqa: BLE001
        return 0
