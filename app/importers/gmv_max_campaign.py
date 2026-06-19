"""TikTok GMV Max "Campaign overview" (By-Day) export importer.

Source: TikTok Ads / GMV Max campaign reporting → Campaign overview → export the
By-Day view. Filename pattern like `Campaign overview data YYYYMMDD - YYYYMMDD.xlsx`.

File layout:
  Row 0  : column headers
  Row 1+ : one row per calendar day
  Last   : a footer TOTAL row whose "By Day" cell is "-" (skipped)

Columns:
  By Day | Cost | SKU orders (Current shop) | Cost per order (Current shop) |
  Gross revenue (Current shop) | ROI (Current shop) | Currency

We persist the three additive values (cost, sku_orders, gross_revenue) per day;
cost-per-order and ROI are derived downstream. Re-uploading is idempotent —
upsert by metric_date, so TikTok's revisions to recent days flow through.
"""
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.importers.base import BaseImporter, ImportResult
from app.models.gmv_max_daily_metric import GmvMaxDailyMetric
from app.models.import_batch import ImportBatch

COL = {
    "date": "By Day",
    "cost": "Cost",
    "sku_orders": "SKU orders (Current shop)",
    "gross_revenue": "Gross revenue (Current shop)",
}


class GmvMaxCampaignImporter(BaseImporter):
    def run(self, path: Path, db: Session, batch: ImportBatch) -> ImportResult:
        df = pd.read_excel(path, dtype=str)
        missing = [c for c in (COL["date"], COL["cost"]) if c not in df.columns]
        if missing:
            raise ValueError(f"GMV Max campaign file missing required columns: {missing}")

        rows = []
        for _, row in df.iterrows():
            day = _parse_date(row.get(COL["date"]))
            if day is None:
                # Blank cells and the footer TOTAL row ("-") are skipped silently.
                continue
            rows.append({
                "metric_date": day,
                "cost": _dec(row.get(COL["cost"])),
                "sku_orders": _int(row.get(COL["sku_orders"])),
                "gross_revenue": _dec(row.get(COL["gross_revenue"])),
            })
        return import_dataframe(pd.DataFrame(rows), db, batch)


def import_dataframe(df: pd.DataFrame, db: Session, batch: ImportBatch) -> ImportResult:
    """Upsert by-day GMV-Max metrics from an already-normalized frame.

    The shared core of both ingestion paths: the CSV/XLSX importer (`run`, which
    reads a file first) and the API sync (`app/services/gmv_max_sync.py`, which
    builds the frame from `/gmv_max/report/get/`). Columns must be normalized to
    `metric_date` (date) / `cost` (Decimal) / `sku_orders` (int) /
    `gross_revenue` (Decimal). Idempotent on `metric_date`: a re-run overwrites
    each day in place, so TikTok's revisions to recent days flow through."""
    result = ImportResult()
    if df.empty:
        return result
    for _, row in df.iterrows():
        day = row["metric_date"]
        try:
            _upsert_row(db, day, row, batch)
            result.rows_imported += 1
        except Exception as exc:  # noqa: BLE001
            result.skip(f"{day}: {exc}")
    return result


def _upsert_row(db: Session, day, row, batch: ImportBatch) -> GmvMaxDailyMetric:
    payload = dict(
        import_batch_id=batch.id,
        metric_date=day,
        cost=row["cost"],
        sku_orders=int(row["sku_orders"]),
        gross_revenue=row["gross_revenue"],
    )
    existing = db.execute(
        select(GmvMaxDailyMetric).where(GmvMaxDailyMetric.metric_date == day)
    ).scalar_one_or_none()
    if existing is None:
        obj = GmvMaxDailyMetric(**payload)
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


def _parse_date(v):
    """Parse the 'By Day' cell. With dtype=str, a date renders as
    '2026-01-01 00:00:00' (or '2026-01-01'); the footer TOTAL row is '-'."""
    s = _str(v)
    if s is None or s in {"-", "—", "–"}:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _dec(v) -> Decimal:
    s = _str(v)
    if s is None or s in {"-", "—", "–"}:
        return Decimal("0")
    try:
        return Decimal(s.replace(",", "").replace("$", ""))
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
