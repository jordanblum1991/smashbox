"""Samples-sent-by-creator report.

Two-pass approach:
  Pass 1 — normalized rows: Sample rows WHERE creator_id IS NOT NULL, grouped by
            creator_id. Creator metadata loaded from the Creator table.
  Pass 2 — legacy rows: Sample rows WHERE creator_id IS NULL, grouped by the raw
            creator_handle string.

A single real person can appear as TWO rows during the transition period before
legacy Sample rows are backfilled with a creator_id. This is intentional and
expected — the is_legacy flag drives a badge in the template, and a one-line
template note explains the split. Do NOT attempt automatic legacy→creator
matching here; that is a separate data-cleanup task.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.creator import Creator
from app.models.sample import Sample
from app.services.sku_alias import load_alias_map


@dataclass
class SamplesByCreatorRow:
    creator_id: int | None            # FK; None for legacy rows
    creator_handle: str | None        # Creator.handle or raw string for legacy
    creator_name: str | None          # Creator.name; None for legacy / unnamed
    platform: str | None              # Creator.platform; None for legacy
    is_legacy: bool                   # True when creator_id is null
    total_samples_sent: int           # sum of Sample.quantity
    distinct_sku_count: int           # distinct canonical SKUs (alias_map applied)
    total_shipping_cost: Decimal | None  # sum of shipping_cost; None if all null
    first_shipped_at: datetime        # earliest shipment
    last_shipped_at: datetime         # most recent shipment


@dataclass
class SamplesByCreatorView:
    rows: list[SamplesByCreatorRow]   # sorted by total_samples_sent desc
    total_samples_sent: int           # grand total across all rows
    creator_count: int                # normalized + legacy row count
    any_shipping_cost: bool           # gates the shipping cost column in template


def compute_samples_by_creator_view(
    db: Session,
    *,
    brand: str | None = None,
    shop_id: int | None = None,
) -> SamplesByCreatorView:
    """Build SamplesByCreatorView from Sample rows, grouped by creator.

    Pass 1: normalized rows (creator_id IS NOT NULL), grouped by creator_id.
    Pass 2: legacy rows (creator_id IS NULL), grouped by creator_handle string.
    Both passes apply alias_map to canonicalize SKUs before counting distinct_sku_count.

    The two passes are concatenated and sorted by total_samples_sent desc.
    """
    alias_map = load_alias_map(db)
    rows: list[SamplesByCreatorRow] = []

    # --- Pass 1: normalized rows (creator_id set) ---
    normalized_agg = db.execute(
        select(
            Sample.creator_id,
            func.sum(Sample.quantity).label("total_qty"),
            func.sum(Sample.shipping_cost).label("total_shipping"),
            func.min(Sample.shipped_at).label("first_shipped"),
            func.max(Sample.shipped_at).label("last_shipped"),
        )
        .where(Sample.creator_id.is_not(None))
        .group_by(Sample.creator_id)
    ).all()

    if normalized_agg:
        creator_ids = [r.creator_id for r in normalized_agg]
        creators_by_id: dict[int, Creator] = {
            c.id: c for c in db.execute(
                select(Creator).where(Creator.id.in_(creator_ids))
            ).scalars()
        }

        # Distinct canonical SKUs per creator_id, applied in Python so alias_map works.
        sku_rows_normalized = db.execute(
            select(Sample.creator_id, Sample.sku)
            .where(Sample.creator_id.in_(creator_ids))
            .distinct()
        ).all()
        sku_sets_by_creator: dict[int, set[str]] = {}
        for cid, sku in sku_rows_normalized:
            canonical = alias_map.get(sku, sku)
            sku_sets_by_creator.setdefault(cid, set()).add(canonical)

        for agg in normalized_agg:
            creator = creators_by_id.get(agg.creator_id)
            shipping = agg.total_shipping
            rows.append(SamplesByCreatorRow(
                creator_id=agg.creator_id,
                creator_handle=creator.handle if creator else None,
                creator_name=creator.name if creator else None,
                platform=creator.platform if creator else None,
                is_legacy=False,
                total_samples_sent=int(agg.total_qty or 0),
                distinct_sku_count=len(sku_sets_by_creator.get(agg.creator_id, set())),
                total_shipping_cost=Decimal(str(shipping)) if shipping is not None else None,
                first_shipped_at=agg.first_shipped,
                last_shipped_at=agg.last_shipped,
            ))

    # --- Pass 2: legacy rows (creator_id IS NULL), grouped by creator_handle ---
    legacy_agg = db.execute(
        select(
            Sample.creator_handle,
            func.sum(Sample.quantity).label("total_qty"),
            func.sum(Sample.shipping_cost).label("total_shipping"),
            func.min(Sample.shipped_at).label("first_shipped"),
            func.max(Sample.shipped_at).label("last_shipped"),
        )
        .where(Sample.creator_id.is_(None))
        .group_by(Sample.creator_handle)
    ).all()

    if legacy_agg:
        # Distinct canonical SKUs per handle.
        legacy_handles = [r.creator_handle for r in legacy_agg if r.creator_handle]
        sku_rows_legacy = db.execute(
            select(Sample.creator_handle, Sample.sku)
            .where(Sample.creator_id.is_(None))
            .where(Sample.creator_handle.in_(legacy_handles))
            .distinct()
        ).all()
        sku_sets_by_handle: dict[str | None, set[str]] = {}
        for handle, sku in sku_rows_legacy:
            canonical = alias_map.get(sku, sku)
            sku_sets_by_handle.setdefault(handle, set()).add(canonical)

        for agg in legacy_agg:
            shipping = agg.total_shipping
            rows.append(SamplesByCreatorRow(
                creator_id=None,
                creator_handle=agg.creator_handle,
                creator_name=None,
                platform=None,
                is_legacy=True,
                total_samples_sent=int(agg.total_qty or 0),
                distinct_sku_count=len(sku_sets_by_handle.get(agg.creator_handle, set())),
                total_shipping_cost=Decimal(str(shipping)) if shipping is not None else None,
                first_shipped_at=agg.first_shipped,
                last_shipped_at=agg.last_shipped,
            ))

    rows.sort(key=lambda r: -r.total_samples_sent)

    total_sent = sum(r.total_samples_sent for r in rows)
    any_shipping = any(r.total_shipping_cost is not None for r in rows)

    return SamplesByCreatorView(
        rows=rows,
        total_samples_sent=total_sent,
        creator_count=len(rows),
        any_shipping_cost=any_shipping,
    )
