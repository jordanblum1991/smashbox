"""Sample inventory on-hand report.

Derives current sample pool balance per SKU from the SampleInventoryMovement
ledger, then enriches each row with catalog metadata (Sku / Bundle).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.bundle import Bundle
from app.models.sku import Sku
from app.services.reporting_tz import now_local
from app.services.sample_service import get_sample_on_hand
from app.services.sku_alias import load_alias_map


@dataclass
class SampleOnHandRow:
    canonical_sku: str       # ledger key from get_sample_on_hand
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
    """Build SampleInventoryView from the ledger, enriched with catalog metadata.

    Calls get_sample_on_hand (which applies alias_map defensively), then looks up
    each canonical SKU key against Sku and Bundle catalog tables. Unmapped keys
    produce a row with sku_code=None, name=None. Sorted: mapped rows by sku_code
    ascending, then unmapped rows at the end.
    """
    alias_map = load_alias_map(db)
    on_hand = get_sample_on_hand(db, brand=brand, shop_id=shop_id, alias_map=alias_map)

    if not on_hand:
        return SampleInventoryView(
            rows=[],
            total_on_hand_units=0,
            sku_count=0,
            as_of=now_local(),
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
        as_of=now_local(),
    )
