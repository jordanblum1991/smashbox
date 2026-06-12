"""Complete inventory report — every SKU we hold stock for, with sellable (SB)
and sample (SBS) on-hand side by side.

Sources both warehouses from their latest SAP snapshot (`InventorySnapshot` and
`SampleInventorySnapshot`), unions the SKU keys, and enriches each with catalog
metadata (Sku / Bundle). Unlike the sample-inventory report, zero-balance SKUs
are KEPT — this is the full picture, including out-of-stock items.

Read by the Inventory Report page + CSV export. Pure computation, no writes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.bundle import Bundle
from app.models.inventory_snapshot import InventorySnapshot
from app.models.sample_inventory_snapshot import SampleInventorySnapshot
from app.models.sku import Sku
from app.services.inventory_sync import last_synced_at
from app.services.reporting_tz import now_local, utc_to_shop_local
from app.services.sku_alias import load_alias_map


@dataclass
class InventoryReportRow:
    canonical_sku: str       # the catalog/snapshot key
    sku_code: str | None     # SBX-form from Sku.sku / Bundle.bundle_sku; None=unmapped
    name: str | None         # product name; None=unmapped
    is_bundle: bool
    sellable_on_hand: int    # SB warehouse
    sample_on_hand: int      # SBS warehouse
    total_on_hand: int       # sellable + sample
    unit_cogs: Decimal       # per-unit cost (Sku.unit_cogs / Bundle.calculated_cogs)
    sellable_value: Decimal  # sellable_on_hand × unit_cogs
    sample_value: Decimal    # sample_on_hand × unit_cogs
    total_value: Decimal     # total_on_hand × unit_cogs


@dataclass
class InventoryReportView:
    rows: list[InventoryReportRow]
    total_sellable: int
    total_sample: int
    total_units: int
    sku_count: int
    total_sellable_value: Decimal
    total_sample_value: Decimal
    total_inventory_value: Decimal
    last_synced_at: datetime | None  # shop-local time of the last SAP sync
    as_of: datetime


def _latest_on_hand(
    db: Session, model, alias_map: dict[str, str],
) -> dict[str, int]:
    """Latest on_hand per SKU for `model`, alias-collapsed to canonical. Keeps
    zero balances (the full report shows out-of-stock SKUs too)."""
    latest_dt = dict(
        db.execute(
            select(model.sku, func.max(model.captured_at)).group_by(model.sku)
        ).all()
    )
    if not latest_dt:
        return {}
    rows = db.execute(
        select(model.sku, model.on_hand, model.captured_at)
        .where(model.sku.in_(latest_dt.keys()))
    ).all()
    out: dict[str, int] = {}
    latest_per_canonical: dict[str, datetime] = {}
    for sku, oh, captured_at in rows:
        if latest_dt[sku] != captured_at:
            continue
        canonical = alias_map.get(sku, sku)
        prev = latest_per_canonical.get(canonical)
        if prev is None or captured_at > prev:
            latest_per_canonical[canonical] = captured_at
            out[canonical] = int(oh or 0)
    return out


def compute_inventory_report(db: Session) -> InventoryReportView:
    alias_map = load_alias_map(db)
    sellable = _latest_on_hand(db, InventorySnapshot, alias_map)
    sample = _latest_on_hand(db, SampleInventorySnapshot, alias_map)
    keys = set(sellable) | set(sample)

    sync_sellable = last_synced_at(db, InventorySnapshot)
    sync_sample = last_synced_at(db, SampleInventorySnapshot)
    last_sync = max([t for t in (sync_sellable, sync_sample) if t], default=None)

    if not keys:
        return InventoryReportView(
            rows=[], total_sellable=0, total_sample=0, total_units=0, sku_count=0,
            total_sellable_value=Decimal("0"), total_sample_value=Decimal("0"),
            total_inventory_value=Decimal("0"),
            last_synced_at=utc_to_shop_local(last_sync) if last_sync else None,
            as_of=now_local(),
        )

    canonical_keys = list(keys)
    sku_by_key: dict[str, Sku] = {}
    for s in db.execute(
        select(Sku).where(
            (Sku.tiktok_sku_id.in_(canonical_keys))
            | (Sku.sku.in_(canonical_keys))
            | (Sku.tiktok_alt_sku.in_(canonical_keys))
        )
    ).scalars():
        for key in (s.tiktok_sku_id, s.sku, s.tiktok_alt_sku):
            if key:
                sku_by_key[str(key)] = s

    bundle_by_key: dict[str, Bundle] = {}
    for b in db.execute(
        select(Bundle)
        .options(selectinload(Bundle.components))  # for calculated_cogs without N+1
        .where(
            (Bundle.tiktok_sku_id.in_(canonical_keys))
            | (Bundle.bundle_sku.in_(canonical_keys))
        )
    ).scalars():
        for key in (b.tiktok_sku_id, b.bundle_sku):
            if key:
                bundle_by_key[str(key)] = b

    mapped: list[InventoryReportRow] = []
    unmapped: list[InventoryReportRow] = []
    for key in keys:
        sell = sellable.get(key, 0)
        samp = sample.get(key, 0)
        sku = sku_by_key.get(key)
        bundle = bundle_by_key.get(key)
        if sku:
            sku_code, name, is_bundle = sku.sku, sku.name, False
            cogs = sku.unit_cogs or Decimal("0")
        elif bundle:
            sku_code, name, is_bundle = bundle.bundle_sku, bundle.name, True
            cogs = bundle.calculated_cogs
        else:
            sku_code, name, is_bundle = None, None, False
            cogs = Decimal("0")
        row = InventoryReportRow(
            canonical_sku=key, sku_code=sku_code, name=name, is_bundle=is_bundle,
            sellable_on_hand=sell, sample_on_hand=samp, total_on_hand=sell + samp,
            unit_cogs=cogs,
            sellable_value=cogs * sell, sample_value=cogs * samp,
            total_value=cogs * (sell + samp),
        )
        (mapped if sku_code else unmapped).append(row)

    mapped.sort(key=lambda r: (r.sku_code or ""))
    rows = mapped + unmapped

    return InventoryReportView(
        rows=rows,
        total_sellable=sum(sellable.values()),
        total_sample=sum(sample.values()),
        total_units=sum(sellable.values()) + sum(sample.values()),
        sku_count=len(keys),
        total_sellable_value=sum((r.sellable_value for r in rows), Decimal("0")),
        total_sample_value=sum((r.sample_value for r in rows), Decimal("0")),
        total_inventory_value=sum((r.total_value for r in rows), Decimal("0")),
        last_synced_at=utc_to_shop_local(last_sync) if last_sync else None,
        as_of=now_local(),
    )
