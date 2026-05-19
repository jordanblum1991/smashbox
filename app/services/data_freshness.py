"""Data-freshness snapshot for the Dashboard.

Reports the most recent COMPLETED import per data kind, so anyone glancing at
the Dashboard can tell at once whether the data is up to date. Catalog kinds
(SKU master, bundle mapping) are skipped — they're reference data, not
transactional, so "staleness" doesn't carry the same meaning.

Each entry returns a small relative-time string ("2h ago", "5d ago", "today")
plus an absolute timestamp for tooltips and a `staleness` bucket so the
template can light up rows that haven't been refreshed in a while.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.import_batch import (
    ImportBatch,
    ImportBatchStatus,
    ImportFileKind,
    _utc_now_naive,
)

# Order matters — this is the order the widget renders them in.
DATA_KINDS: tuple[tuple[ImportFileKind, str], ...] = (
    (ImportFileKind.TIKTOK_ORDERS, "Orders"),
    (ImportFileKind.TIKTOK_SETTLEMENTS, "Settlements"),
    (ImportFileKind.TIKTOK_PAYOUTS, "Payouts"),
    (ImportFileKind.TIKTOK_ADS, "Ad spend"),
    (ImportFileKind.SAMPLES, "Samples"),
)


@dataclass
class FreshnessEntry:
    kind: ImportFileKind
    label: str                       # human-readable, e.g. "Orders"
    last_imported_at: datetime | None
    relative: str                    # "2h ago" / "today" / "—"
    staleness: str                   # "fresh" | "stale" | "missing"


def _relative(ts: datetime, now: datetime) -> str:
    delta = now - ts
    secs = int(delta.total_seconds())
    if secs < 60:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    days = delta.days
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days}d ago"
    if days < 30:
        weeks = days // 7
        return f"{weeks}w ago"
    return ts.strftime("%b %d, %Y")


def _staleness(ts: datetime | None, now: datetime) -> str:
    if ts is None:
        return "missing"
    return "stale" if (now - ts) > timedelta(days=7) else "fresh"


def compute_freshness(db: Session) -> list[FreshnessEntry]:
    now = _utc_now_naive()
    out: list[FreshnessEntry] = []
    for kind, label in DATA_KINDS:
        ts = db.execute(
            select(ImportBatch.uploaded_at)
            .where(ImportBatch.kind == kind)
            .where(ImportBatch.status == ImportBatchStatus.COMPLETED)
            .order_by(ImportBatch.uploaded_at.desc())
            .limit(1)
        ).scalar()
        out.append(FreshnessEntry(
            kind=kind,
            label=label,
            last_imported_at=ts,
            relative=_relative(ts, now) if ts else "—",
            staleness=_staleness(ts, now),
        ))
    return out
