"""Complete inventory report — every SKU we hold stock for, with sellable (SB)
and sample (SBS) on-hand side by side.

Sources both warehouses from their latest SAP snapshot (`InventorySnapshot` and
`SampleInventorySnapshot`), unions the SKU keys, and enriches each with catalog
metadata (Sku / Bundle). Unlike the sample-inventory report, zero-balance SKUs
are KEPT — this is the full picture, including out-of-stock items.

Rows that share a base product name but differ only by shade/size (each its own
SBX code + own on-hand) are rolled up into one expandable **family group** so the
list is scannable; single products, bundles, and unmapped keys stay flat. Each
row/group also carries a stock-status badge + days-of-cover, sourced from the
demand planner so the inventory report agrees with the Demand Planning page.

Read by the Inventory Report page + CSV export. Pure computation, no writes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.bundle import Bundle
from app.models.inventory_snapshot import InventorySnapshot
from app.models.sample_inventory_snapshot import SampleInventorySnapshot
from app.models.sku import Sku
from app.reports.in_transit import compute_in_transit
from app.services.demand.replenishment import ReplenishmentStatus
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
    in_transit: int          # units on placed (un-received) purchase orders
    unit_cogs: Decimal       # per-unit cost (Sku.unit_cogs / Bundle.calculated_cogs)
    sellable_value: Decimal  # sellable_on_hand × unit_cogs
    sample_value: Decimal    # always 0 — sample stock carries $0 COGS (expensed when given)
    total_value: Decimal     # sellable_on_hand × unit_cogs (samples excluded from value)
    # Enrichment from the demand planner (defaults keep direct constructors working).
    status: str = "none"     # badge: out | low | healthy | overstock | none
    days_of_cover: Decimal | None = None  # sellable days of supply; None = no sales signal


@dataclass
class InventoryGroup:
    """One displayed line. A *family* (is_family=True) summarizes several shade/size
    members and expands to show them; a singleton wraps one row and renders flat."""
    key: str
    label: str               # display name (base product name / sku / canonical)
    sku_code: str | None      # representative SBX code; None when unmapped
    is_family: bool
    is_bundle: bool
    members: list[InventoryReportRow]
    member_count: int
    sellable_on_hand: int
    sample_on_hand: int
    total_on_hand: int
    in_transit: int
    unit_cogs: Decimal | None  # None when members' per-unit cost differs (a family)
    sellable_value: Decimal
    sample_value: Decimal
    total_value: Decimal
    status: str               # worst member badge (so a stockout can't hide collapsed)
    days_of_cover: Decimal | None


@dataclass
class InventoryReportView:
    rows: list[InventoryReportRow]          # flat, per-member (CSV / xlsx / email read this)
    total_sellable: int
    total_sample: int
    total_units: int
    total_in_transit: int
    sku_count: int
    total_sellable_value: Decimal
    total_sample_value: Decimal
    total_inventory_value: Decimal
    last_synced_at: datetime | None  # shop-local time of the last SAP sync
    as_of: datetime
    # Rolled-up, ordered for display (the page reads this). Optional/last so
    # callers that only need the flat `rows` (email/xlsx renderers, their tests)
    # can construct a view without it.
    groups: list[InventoryGroup] = field(default_factory=list)


# ---- family-key derivation + status badges --------------------------------

_TRAILING_SIZE_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _strip_trailing_size(name: str) -> str:
    """Drop a trailing parenthesized size chunk, e.g. ' (1.7 oz)'."""
    return _TRAILING_SIZE_RE.sub("", str(name))


def _family_label(name: str) -> str:
    """Base product name: size paren removed and a trailing ' - <shade>' segment
    dropped. Splits only on ' - ' (spaces required) so internal hyphens like
    'ALL-IN-ONE' / 'ANTI-REDNESS' are preserved."""
    base = _strip_trailing_size(name)
    base = base.rsplit(" - ", 1)[0]
    return re.sub(r"\s+", " ", base).strip()


def _family_key(name: str | None) -> str | None:
    """Normalized grouping key for a product name (None when there's no name)."""
    if not name:
        return None
    return (_family_label(name).upper() or None)


_BADGE_FOR_STATUS = {
    ReplenishmentStatus.OUT_OF_STOCK: "out",
    ReplenishmentStatus.AT_RISK: "low",
    ReplenishmentStatus.REORDER_NOW: "low",
    ReplenishmentStatus.HEALTHY: "healthy",
    ReplenishmentStatus.OVERSTOCKED: "overstock",
    ReplenishmentStatus.NO_VELOCITY: "none",
    ReplenishmentStatus.DISCONTINUED: "none",
}

# Badge severity for "most urgent wins" + default sort (lower = more urgent).
_BADGE_SEVERITY = {"out": 0, "low": 1, "healthy": 2, "overstock": 3, "none": 4}


def _badge_for(status: ReplenishmentStatus | None) -> str:
    """Map a planner status (or None) to an inventory stock-status badge."""
    if status is None:
        return "none"
    return _BADGE_FOR_STATUS.get(status, "none")


def _worst_badge(badges: list[str]) -> str:
    """Most-urgent badge in the list (empty → 'none')."""
    if not badges:
        return "none"
    return min(badges, key=lambda b: _BADGE_SEVERITY.get(b, 4))


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


def _planner_index(db: Session) -> dict:
    """`{physical Sku.sku: ReplenishmentResult}` from the demand planner, used for
    per-row status + days-of-cover. Wrapped so a planner failure degrades the
    inventory report to no-badge rather than breaking it."""
    try:
        from app.reports.demand_planning import compute_demand_planning_view
        view = compute_demand_planning_view(db)
        return {r.component_sku: r for r in view.rows}
    except Exception:  # noqa: BLE001 — status is best-effort enrichment
        return {}


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
            rows=[], groups=[], total_sellable=0, total_sample=0, total_units=0,
            total_in_transit=0, sku_count=0,
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

    # Units on order (placed, un-received POs). compute_in_transit replicates each
    # qty under every catalog identifier, so a lookup by any of a row's keys hits.
    in_transit_map = compute_in_transit(db)

    def _in_transit_for(key: str, sku: Sku | None, bundle: Bundle | None) -> int:
        cands = [key]
        if sku:
            cands += [sku.tiktok_sku_id, sku.sku, sku.tiktok_alt_sku]
        if bundle:
            cands += [bundle.tiktok_sku_id, bundle.bundle_sku]
        for c in cands:
            if c and str(c).strip() in in_transit_map:
                return in_transit_map[str(c).strip()]
        return 0

    # Per-physical-SKU status + days-of-cover from the planner (best-effort).
    planner = _planner_index(db)
    daily_v_by_key: dict[str, Decimal] = {}

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
        # Sample stock carries $0 COGS (it was expensed when given out), so it
        # adds physical units but NOTHING to inventory value. Value = sellable only.
        sellable_value = cogs * sell
        pr = planner.get(sku_code) if sku_code else None
        row = InventoryReportRow(
            canonical_sku=key, sku_code=sku_code, name=name, is_bundle=is_bundle,
            sellable_on_hand=sell, sample_on_hand=samp, total_on_hand=sell + samp,
            in_transit=_in_transit_for(key, sku, bundle),
            unit_cogs=cogs,
            sellable_value=sellable_value, sample_value=Decimal("0"),
            total_value=sellable_value,
            status=_badge_for(pr.status if pr else None),
            days_of_cover=(pr.days_of_supply if pr else None),
        )
        daily_v_by_key[key] = pr.daily_velocity if pr else Decimal("0")
        (mapped if sku_code else unmapped).append(row)

    mapped.sort(key=lambda r: (r.sku_code or ""))
    rows = mapped + unmapped

    groups = _build_groups(rows, daily_v_by_key)

    return InventoryReportView(
        rows=rows,
        groups=groups,
        total_sellable=sum(sellable.values()),
        total_sample=sum(sample.values()),
        total_units=sum(sellable.values()) + sum(sample.values()),
        total_in_transit=sum((r.in_transit for r in rows), 0),
        sku_count=len(keys),
        total_sellable_value=sum((r.sellable_value for r in rows), Decimal("0")),
        total_sample_value=sum((r.sample_value for r in rows), Decimal("0")),
        total_inventory_value=sum((r.total_value for r in rows), Decimal("0")),
        last_synced_at=utc_to_shop_local(last_sync) if last_sync else None,
        as_of=now_local(),
    )


def _build_groups(
    rows: list[InventoryReportRow], daily_v_by_key: dict[str, Decimal],
) -> list[InventoryGroup]:
    """Fold rows that share a shade/size family key into one expandable group.
    Mapped non-bundle rows group by `_family_key(name)`; everything else (single
    products, bundles, unmapped) keys on its own canonical SKU → renders flat."""
    by_key: dict[str, list[InventoryReportRow]] = {}
    order: list[str] = []
    for r in rows:
        fam = _family_key(r.name) if (r.sku_code and not r.is_bundle) else None
        gkey = fam if fam else f"\x00solo:{r.canonical_sku}"
        if gkey not in by_key:
            by_key[gkey] = []
            order.append(gkey)
        by_key[gkey].append(r)

    groups: list[InventoryGroup] = []
    for gkey in order:
        members = by_key[gkey]
        is_family = len(members) > 1
        first = members[0]
        sell = sum(m.sellable_on_hand for m in members)
        samp = sum(m.sample_on_hand for m in members)
        tot = sum(m.total_on_hand for m in members)
        it = sum(m.in_transit for m in members)
        sval = sum((m.sellable_value for m in members), Decimal("0"))
        smval = sum((m.sample_value for m in members), Decimal("0"))
        tval = sum((m.total_value for m in members), Decimal("0"))

        if is_family:
            label = _family_label(first.name)
            cogs_set = {m.unit_cogs for m in members}
            unit_cogs = members[0].unit_cogs if len(cogs_set) == 1 else None
            status = _worst_badge([m.status for m in members])
            total_v = sum((daily_v_by_key.get(m.canonical_sku, Decimal("0"))
                           for m in members), Decimal("0"))
            cover = (Decimal(sell) / total_v).quantize(Decimal("0.1")) if total_v > 0 else None
        else:
            label = first.name or first.canonical_sku
            unit_cogs = first.unit_cogs
            status = first.status
            cover = first.days_of_cover

        groups.append(InventoryGroup(
            key=gkey, label=label, sku_code=first.sku_code,
            is_family=is_family, is_bundle=first.is_bundle,
            members=members, member_count=len(members),
            sellable_on_hand=sell, sample_on_hand=samp, total_on_hand=tot,
            in_transit=it, unit_cogs=unit_cogs,
            sellable_value=sval, sample_value=smval, total_value=tval,
            status=status, days_of_cover=cover,
        ))

    # Default order: most-urgent first, then alphabetical by label.
    groups.sort(key=lambda g: (_BADGE_SEVERITY.get(g.status, 4), (g.label or "").lower()))
    return groups
