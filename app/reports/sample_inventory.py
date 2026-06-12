"""Sample inventory on-hand report.

On-hand per SKU comes from the latest **SAP sample-pool snapshot** (the SBS
warehouse, written to `SampleInventorySnapshot` by the inventory sync) — SAP is
the source of truth for sample stock. Each row is then enriched with catalog
metadata (Sku / Bundle). (The older `SampleInventoryMovement` ledger remains for
receipt/shipment audit but no longer drives this report.)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.bundle import Bundle
from app.models.sample_inventory_snapshot import SampleInventorySnapshot
from app.models.sku import Sku
from app.services.reporting_tz import now_local
from app.services.sku_alias import load_alias_map


def _latest_sample_on_hand(
    db: Session, *, alias_map: dict[str, str] | None = None,
) -> tuple[dict[str, int], datetime | None]:
    """Most-recent sample `on_hand` per SKU, alias-collapsed to canonical, plus
    the global latest `captured_at`. Mirrors the demand planner's sellable
    equivalent: among snapshots whose alias points to the same canonical, the
    most-recently-captured one wins. Zero-balance SKUs are dropped (the report
    only lists stock on hand)."""
    latest_dt_per_sku = dict(
        db.execute(
            select(SampleInventorySnapshot.sku, func.max(SampleInventorySnapshot.captured_at))
            .group_by(SampleInventorySnapshot.sku)
        ).all()
    )
    if not latest_dt_per_sku:
        return {}, None

    rows = db.execute(
        select(SampleInventorySnapshot.sku, SampleInventorySnapshot.on_hand,
               SampleInventorySnapshot.captured_at)
        .where(SampleInventorySnapshot.sku.in_(latest_dt_per_sku.keys()))
    ).all()
    alias_map = alias_map or {}
    on_hand: dict[str, int] = {}
    latest_per_canonical: dict[str, datetime] = {}
    for sku, oh, captured_at in rows:
        if latest_dt_per_sku[sku] != captured_at:
            continue
        canonical = alias_map.get(sku, sku)
        prev = latest_per_canonical.get(canonical)
        if prev is None or captured_at > prev:
            latest_per_canonical[canonical] = captured_at
            on_hand[canonical] = int(oh or 0)

    on_hand = {k: v for k, v in on_hand.items() if v > 0}  # only positive stock
    global_latest = max(latest_dt_per_sku.values())
    return on_hand, global_latest


@dataclass
class SampleOnHandRow:
    canonical_sku: str       # canonical SKU key from the latest sample snapshot
    sku_code: str | None     # SBX-form from Sku.sku or Bundle.bundle_sku; None=unmapped
    name: str | None         # product name; None=unmapped
    is_bundle: bool          # True when matched to Bundle
    on_hand_units: int       # current ledger balance


@dataclass
class SampleInventoryView:
    rows: list[SampleOnHandRow]  # mapped rows sorted by sku_code asc, unmapped last
    total_on_hand_units: int     # headline KPI: sum of all on_hand_units
    sku_count: int               # distinct SKUs with positive stock
    as_of: datetime              # snapshot timestamp


def compute_sample_inventory_view(
    db: Session,
    *,
    brand: str | None = None,
    shop_id: int | None = None,
) -> SampleInventoryView:
    """Build SampleInventoryView from the latest SAP sample snapshot, enriched
    with catalog metadata.

    Reads the most-recent SBS on-hand per SKU (alias-collapsed), then looks up
    each canonical SKU key against Sku and Bundle catalog tables. Unmapped keys
    produce a row with sku_code=None, name=None. Sorted: mapped rows by sku_code
    ascending, then unmapped rows at the end.
    """
    alias_map = load_alias_map(db)
    on_hand, latest_at = _latest_sample_on_hand(db, alias_map=alias_map)

    if not on_hand:
        return SampleInventoryView(
            rows=[],
            total_on_hand_units=0,
            sku_count=0,
            as_of=latest_at or now_local(),
        )

    canonical_skus = list(on_hand.keys())

    sku_by_key: dict[str, Sku] = {}
    for s in db.execute(
        select(Sku).where(
            (Sku.tiktok_sku_id.in_(canonical_skus))
            | (Sku.sku.in_(canonical_skus))
            | (Sku.tiktok_alt_sku.in_(canonical_skus))
        )
    ).scalars():
        for key in (s.tiktok_sku_id, s.sku, s.tiktok_alt_sku):
            if key:
                sku_by_key[str(key)] = s

    bundle_by_key: dict[str, Bundle] = {}
    for b in db.execute(
        select(Bundle).where(
            (Bundle.tiktok_sku_id.in_(canonical_skus))
            | (Bundle.bundle_sku.in_(canonical_skus))
        )
    ).scalars():
        for key in (b.tiktok_sku_id, b.bundle_sku):
            if key:
                bundle_by_key[str(key)] = b

    mapped_rows: list[SampleOnHandRow] = []
    unmapped_rows: list[SampleOnHandRow] = []

    for canonical_sku, units in on_hand.items():
        sku = sku_by_key.get(canonical_sku)
        bundle = bundle_by_key.get(canonical_sku)
        if sku:
            row = SampleOnHandRow(
                canonical_sku=canonical_sku,
                sku_code=sku.sku,
                name=sku.name,
                is_bundle=False,
                on_hand_units=units,
            )
            mapped_rows.append(row)
        elif bundle:
            row = SampleOnHandRow(
                canonical_sku=canonical_sku,
                sku_code=bundle.bundle_sku,
                name=bundle.name,
                is_bundle=True,
                on_hand_units=units,
            )
            mapped_rows.append(row)
        else:
            unmapped_rows.append(SampleOnHandRow(
                canonical_sku=canonical_sku,
                sku_code=None,
                name=None,
                is_bundle=False,
                on_hand_units=units,
            ))

    mapped_rows.sort(key=lambda r: (r.sku_code or ""))
    rows = mapped_rows + unmapped_rows

    return SampleInventoryView(
        rows=rows,
        total_on_hand_units=sum(on_hand.values()),
        sku_count=len(on_hand),
        as_of=latest_at or now_local(),
    )
