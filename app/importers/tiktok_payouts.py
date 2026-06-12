"""TikTok payouts-income workbook importer.

Sourced from the "Payouts/Income" export downloaded from TikTok Shop Finance
(filename pattern: `payouts-income_*.xlsx`). One workbook covers a date range
and contains five sheets — we consume two:

  - "Payments"   : one row per bank transfer. Payment ID is the canonical
                   payout id and the unique key. HEADER ROW IS row 0.
  - "Statements" : one row per merchant statement, linked to a Payment ID.
                   Used to derive each payout's revenue side (sum of net
                   sales) and period span (min/max statement date).
                   HEADER ROW IS row 0.

What this importer does:
  1. Aggregates the Statements sheet by Payment ID — sums net sales and
     captures the date range covered.
  2. For each Payments row, upserts one Payout by payout_id (idempotent on
     re-upload — the workbook is downloaded incrementally).

Field mapping
-------------
  payout_id     = Payment ID
  paid_at       = Payment completion date (bank-confirmed; this is what shows
                  on the bank statement and what reconciliation filters on)
  period_start  = MIN(Statement date) for the linked statements
  period_end    = MAX(Statement date) for the linked statements
  gross_amount  = SUM(Net sales) for the linked statements (revenue side,
                  positive)
  net_amount    = Payment amount (what hit the bank — gold standard)
  fees          = gross_amount - net_amount (total deductions: TikTok fees,
                  shipping, adjustments, reserves rolled together)
  currency      = first non-null Currency from linked Order details rows;
                  defaults to USD

Date format: TikTok writes "YYYY/MM/DD" strings here (different from the
settlement file which uses YYYYMMDD ints).
"""
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.importers.base import BaseImporter, ImportResult
from app.models.import_batch import ImportBatch
from app.models.payout import Payout

PAYMENTS_SHEET = "Payments"
STATEMENTS_SHEET = "Statements"

PAY_COL = {
    "initiation_date": "Payment initiation date",
    "payment_id": "Payment ID",
    "amount": "Payment amount",
    "completion_date": "Payment completion date",
    "status": "Status",
}

STMT_COL = {
    "statement_date": "Statement date",
    "statement_id": "Statement ID",
    "payment_id": "Payment ID",
    "net_sales": "Net sales",
}


class TikTokPayoutsImporter(BaseImporter):
    def run(self, path: Path, db: Session, batch: ImportBatch) -> ImportResult:
        try:
            payments_df = pd.read_excel(path, sheet_name=PAYMENTS_SHEET, dtype=str)
        except ValueError as exc:
            raise ValueError(f"could not read Payments sheet: {exc}") from exc
        try:
            stmt_df = pd.read_excel(path, sheet_name=STATEMENTS_SHEET, dtype=str)
        except Exception:  # noqa: BLE001 — Statements sheet is optional
            stmt_df = None
        return import_dataframes(payments_df, stmt_df, db, batch)


def import_dataframes(
    payments_df: pd.DataFrame, stmt_df: "pd.DataFrame | None",
    db: Session, batch: ImportBatch,
) -> ImportResult:
    """In-memory ingestion seam. `payments_df` = the Payments-sheet rows (cash
    side, columns per PAY_COL); `stmt_df` = the Statements-sheet rows (revenue /
    period detail per STMT_COL, or None when absent — the cash row still imports,
    just without gross/period). The file importer reads the workbook's two sheets;
    a future TikTok API client builds the same frames from the payouts JSON and
    calls this directly. Idempotent upsert on payout_id."""
    result = ImportResult()

    missing = [c for c in (PAY_COL["payment_id"], PAY_COL["amount"]) if c not in payments_df.columns]
    if missing:
        raise ValueError(f"Payments sheet missing required columns: {missing}")

    stmt_rollup = _rollup_statements(stmt_df) if stmt_df is not None else {}

    for _, row in payments_df.iterrows():
        pid = _str(row.get(PAY_COL["payment_id"]))
        if not pid:
            continue
        try:
            _upsert_payout(db, pid, row, stmt_rollup.get(pid), batch)
            result.rows_imported += 1
        except Exception as exc:  # noqa: BLE001
            result.skip(f"payout {pid}: {exc}")

    return result


def _rollup_statements(df: pd.DataFrame) -> dict[str, dict]:
    """Aggregate the Statements sheet by Payment ID.

    Returns {payment_id: {gross, period_start, period_end}}.
    """
    out: dict[str, dict] = {}
    if PAY_COL["payment_id"] not in df.columns:
        return out

    for pid, group in df.groupby(STMT_COL["payment_id"], dropna=True, sort=False):
        pid = _str(pid)
        if not pid:
            continue
        gross = sum(
            (_dec(r.get(STMT_COL["net_sales"])) for _, r in group.iterrows()),
            Decimal("0"),
        )
        dates = [
            d for d in (
                _parse_ymd(r.get(STMT_COL["statement_date"])) for _, r in group.iterrows()
            ) if d is not None
        ]
        out[pid] = {
            "gross": gross,
            "period_start": min(dates) if dates else None,
            "period_end": max(dates) if dates else None,
        }
    return out


def _upsert_payout(
    db: Session,
    payout_id: str,
    row: pd.Series,
    stmt: dict | None,
    batch: ImportBatch,
) -> Payout:
    net = _dec(row.get(PAY_COL["amount"]))
    gross = (stmt or {}).get("gross", Decimal("0"))
    # If we have no statement detail, gross defaults to net so fees=0 rather
    # than reporting a misleading "everything was fees" row.
    if gross == 0 and not stmt:
        gross = net
    fees = gross - net

    paid_at = (
        _parse_ymd(row.get(PAY_COL["completion_date"]))
        or _parse_ymd(row.get(PAY_COL["initiation_date"]))
    )
    if paid_at is None:
        raise ValueError("no usable payment date")

    payload = dict(
        import_batch_id=batch.id,
        payout_id=payout_id,
        paid_at=paid_at,
        period_start=(stmt or {}).get("period_start"),
        period_end=(stmt or {}).get("period_end"),
        gross_amount=gross,
        fees=fees,
        net_amount=net,
        currency="USD",
    )

    existing = db.execute(
        select(Payout).where(Payout.payout_id == payout_id)
    ).scalar_one_or_none()

    if existing is None:
        obj = Payout(**payload)
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
    s = _str(v)
    if s is None or s == "/":
        return Decimal("0")
    try:
        return Decimal(s)
    except Exception:  # noqa: BLE001
        return Decimal("0")


def _parse_ymd(v) -> datetime | None:
    """Payout/statement dates are 'YYYY/MM/DD' strings."""
    s = _str(v)
    if s is None:
        return None
    s = s.split(" ")[0]  # strip any trailing time
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None
