"""TikTok Ads Manager "Cost" export importer.

Source: TikTok Ads Manager → Reporting → Cost export
(filename pattern: `Cost_<advertiser_id>_<timestamp>.xlsx`).

Layout (single sheet, header on row 0):
  Date | Campaign name | Campaign ID | Cash cost | Credit cost |
  Ad credit cost | Amount | Currency | Type

One row per (date, campaign). A trailing "Total" footer row exists in some
exports — skipped on import.

Sign convention: TikTok writes ad costs as NEGATIVE numbers. We store the
absolute magnitude on AdSpend.amount so the P&L renderer can subtract
directly (matches Order.shop_ads_cost).

Idempotency: re-uploading the same file or an overlapping refresh is a
no-op upsert keyed on (spend_date, campaign_id).
"""
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.importers.base import BaseImporter, ImportResult
from app.models.ad_spend import AdSpend
from app.models.import_batch import ImportBatch

COL = {
    "date": "Date",
    "campaign_name": "Campaign name",
    "campaign_id": "Campaign ID",
    "cash_cost": "Cash cost",
    "credit_cost": "Credit cost",
    "ad_credit_cost": "Ad credit cost",
    "amount": "Amount",
    "currency": "Currency",
    "type": "Type",
}

REQUIRED = (COL["date"], COL["campaign_id"], COL["amount"])


class TikTokAdsImporter(BaseImporter):
    def run(self, path: Path, db: Session, batch: ImportBatch) -> ImportResult:
        result = ImportResult()

        df = pd.read_excel(path, dtype=str)
        missing = [c for c in REQUIRED if c not in df.columns]
        if missing:
            raise ValueError(f"Cost file missing required columns: {missing}")

        for _, row in df.iterrows():
            raw_date = _str(row.get(COL["date"]))
            if raw_date is None or raw_date.lower() == "total":
                continue
            campaign_id = _str(row.get(COL["campaign_id"]))
            if not campaign_id:
                result.skip(f"row missing Campaign ID: date={raw_date}")
                continue
            spend_date = _parse_date(raw_date)
            if spend_date is None:
                result.skip(f"unparseable date: {raw_date!r}")
                continue
            try:
                _upsert(db, spend_date, campaign_id, row, batch)
                result.rows_imported += 1
            except Exception as exc:  # noqa: BLE001
                result.skip(f"{spend_date.date()} / {campaign_id}: {exc}")

        return result


def _upsert(
    db: Session,
    spend_date: datetime,
    campaign_id: str,
    row: pd.Series,
    batch: ImportBatch,
) -> AdSpend:
    return _persist(db, spend_date, campaign_id, batch, dict(
        campaign_name=_str(row.get(COL["campaign_name"])),
        cash_cost=_abs(row.get(COL["cash_cost"])),
        credit_cost=_abs(row.get(COL["credit_cost"])),
        ad_credit_cost=_abs(row.get(COL["ad_credit_cost"])),
        amount=_abs(row.get(COL["amount"])),
        currency=_str(row.get(COL["currency"])) or "USD",
        campaign_type=_str(row.get(COL["type"])),
    ))


def _persist(
    db: Session,
    spend_date: datetime,
    campaign_id: str,
    batch: ImportBatch,
    fields: dict,
) -> AdSpend:
    """Upsert one AdSpend row on the (spend_date, campaign_id) natural key.
    Shared by the file importer and the Marketing API fetcher seam below."""
    payload = dict(
        import_batch_id=batch.id,
        spend_date=spend_date,
        campaign_id=campaign_id,
        **fields,
    )
    existing = db.execute(
        select(AdSpend)
        .where(AdSpend.spend_date == spend_date)
        .where(AdSpend.campaign_id == campaign_id)
    ).scalar_one_or_none()

    if existing is None:
        obj = AdSpend(**payload)
        db.add(obj)
        return obj
    for k, v in payload.items():
        setattr(existing, k, v)
    return existing


def import_ad_spend_rows(rows: list[dict], db: Session, batch: ImportBatch) -> ImportResult:
    """In-memory seam: feed pre-mapped ad-spend rows (from the Marketing API
    fetcher) through the SAME upsert as the file importer. Each row is a dict:
    {spend_date: datetime, campaign_id: str, campaign_name, cash_cost,
    credit_cost, ad_credit_cost, amount: Decimal, currency, campaign_type}.
    Idempotent on (spend_date, campaign_id). Returns ImportResult."""
    result = ImportResult()
    for r in rows:
        spend_date = r.get("spend_date")
        campaign_id = (r.get("campaign_id") or "").strip()
        if spend_date is None or not campaign_id:
            result.skip(f"row missing spend_date/campaign_id: {r!r}")
            continue
        try:
            _persist(db, spend_date, campaign_id, batch, {
                "campaign_name": r.get("campaign_name"),
                "cash_cost": r.get("cash_cost", Decimal("0")),
                "credit_cost": r.get("credit_cost", Decimal("0")),
                "ad_credit_cost": r.get("ad_credit_cost", Decimal("0")),
                "amount": r.get("amount", Decimal("0")),
                "currency": r.get("currency") or "USD",
                "campaign_type": r.get("campaign_type"),
            })
            result.rows_imported += 1
        except Exception as exc:  # noqa: BLE001
            result.skip(f"{spend_date} / {campaign_id}: {exc}")
    return result


def _str(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return None
    return s


def _abs(v) -> Decimal:
    """Parse a money cell and return its magnitude (TikTok writes costs as
    negative numbers; we store positives so the P&L can subtract directly)."""
    s = _str(v)
    if s is None:
        return Decimal("0")
    try:
        return abs(Decimal(s))
    except Exception:  # noqa: BLE001
        return Decimal("0")


def _parse_date(s: str) -> datetime | None:
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s.split(" ")[0], fmt)
        except ValueError:
            continue
    return None
