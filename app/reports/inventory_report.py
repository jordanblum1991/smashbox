"""Complete inventory report — every SKU we hold stock for, with sellable (SB)
and sample (SBS) on-hand side by side.

Sources both warehouses from their latest SAP snapshot (`InventorySnapshot` and
`SampleInventorySnapshot`), unions the SKU keys, and enriches each with catalog
metadata (Sku / Bundle). Unlike the sample-inventory report, zero-balance SKUs
are KEPT — this is the full picture, including out-of-stock items.

Shades of one product share an SBX code base (differing only by a trailing
2-digit shade number) and each carries its own on-hand; they roll up into one
expandable **family group** keyed on that code base so the list is scannable.
Single products, bundles, size/format variants, and unmapped keys stay flat. Each
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
from app.reports.sample_inbound import compute_sample_inbound, sample_inbound_summary
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
    family_override: str | None = None    # manual Sku.family — overrides the auto family key
    sample_in_transit: int = 0            # open sample inbound orders (email column)


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
    status_count: int         # how many members are in that worst status (e.g. "Out ·1")
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
    total_sample_in_transit: int = 0        # open sample inbound units (email)


# ---- family-key derivation + status badges --------------------------------

_TRAILING_SIZE_RE = re.compile(r"\s*\([^)]*\)\s*$")
_SHADE_SUFFIX_RE = re.compile(r"\d{2}$")


def _strip_trailing_size(name: str) -> str:
    """Drop a trailing parenthesized size chunk, e.g. ' (1.7 oz)'."""
    return _TRAILING_SIZE_RE.sub("", str(name))


def _family_key(sku_code: str | None) -> str | None:
    """Family key = the SBX code base with its trailing 2-digit shade number
    removed (e.g. 'SBX-C5JK01' -> 'SBX-C5JK'). Returns None when the code has no
    2-digit shade suffix — that SKU isn't part of a shade range and renders flat.

    The product NAME is deliberately NOT used: real shade names are delimited
    inconsistently (' - ', bare '-', 'MINI-', spaces), so name parsing both
    misses big ranges and risks false merges. The code base is the clean signal.
    Size/format variants (mini, jumbo) carry a different code base and so stay
    separate by design."""
    if not sku_code:
        return None
    base = _SHADE_SUFFIX_RE.sub("", sku_code)
    return base if base != sku_code else None


def _common_label(names: list[str]) -> str:
    """Family display label: the longest shared leading run of words across the
    member names (trailing size paren ignored). Falls back to the first cleaned
    name when the members share no leading words."""
    cleaned = [_strip_trailing_size(n).split() for n in names if n]
    if not cleaned:
        return ""
    prefix: list[str] = []
    for tup in zip(*cleaned):
        if all(w == tup[0] for w in tup):
            prefix.append(tup[0])
        else:
            break
    label = " ".join(prefix) if prefix else (
        _strip_trailing_size(names[0]) if names and names[0] else "")
    # Drop a dangling shade delimiter ("Lipstick -" / "Highlighter-") left when
    # the members share the separator but diverge at the shade itself.
    return label.strip().rstrip(" -–—·,").strip()


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
    in_transit_map = compute_in_transit(db)               # sellable, placed POs
    sample_inbound_map = compute_sample_inbound(db)        # sample, open inbound orders

    def _units_for(m: dict[str, int], key: str, sku: Sku | None, bundle: Bundle | None) -> int:
        cands = [key]
        if sku:
            cands += [sku.tiktok_sku_id, sku.sku, sku.tiktok_alt_sku]
        if bundle:
            cands += [bundle.tiktok_sku_id, bundle.bundle_sku]
        for c in cands:
            if c and str(c).strip() in m:
                return m[str(c).strip()]
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
            in_transit=_units_for(in_transit_map, key, sku, bundle),
            sample_in_transit=_units_for(sample_inbound_map, key, sku, bundle),
            unit_cogs=cogs,
            sellable_value=sellable_value, sample_value=Decimal("0"),
            total_value=sellable_value,
            status=_badge_for(pr.status if pr else None),
            days_of_cover=(pr.days_of_supply if pr else None),
            family_override=((sku.family or None) if sku else None),
        )
        daily_v_by_key[key] = pr.daily_velocity if pr else Decimal("0")
        (mapped if sku_code else unmapped).append(row)

    # Drop catalog-gap noise: rows not in the catalog (unmapped) that also have
    # no stock and nothing on order. SAP keeps feeding discontinued zero-stock
    # codes; with no catalog row they're pure noise. Mapped out-of-stock products
    # and unmapped rows that still hold/expect stock are kept.
    unmapped = [r for r in unmapped if r.total_on_hand > 0 or r.in_transit > 0]

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
        total_sample_in_transit=sample_inbound_summary(db)["units_inbound"],
        sku_count=len(rows),
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
        if r.sku_code and not r.is_bundle:
            # Manual Sku.family wins over the auto code-base rule.
            fam = r.family_override or _family_key(r.sku_code)
        else:
            fam = None
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
            # A manual-family group is labeled by the family value the operator
            # typed; an auto code-base group derives a label from member names.
            label = first.family_override or _common_label([m.name or "" for m in members])
            code_display = gkey  # the shared code base, e.g. "SBX-C5JK"
            cogs_set = {m.unit_cogs for m in members}
            unit_cogs = members[0].unit_cogs if len(cogs_set) == 1 else None
            member_badges = [m.status for m in members]
            status = _worst_badge(member_badges)
            # How many shades are in that worst status — so "Out ·1" reads as
            # "1 shade out", not "the whole family is out".
            status_count = sum(1 for badge in member_badges if badge == status)
            total_v = sum((daily_v_by_key.get(m.canonical_sku, Decimal("0"))
                           for m in members), Decimal("0"))
            cover = (Decimal(sell) / total_v).quantize(Decimal("0.1")) if total_v > 0 else None
        else:
            label = first.name or first.canonical_sku
            code_display = first.sku_code
            unit_cogs = first.unit_cogs
            status = first.status
            status_count = 1
            cover = first.days_of_cover

        groups.append(InventoryGroup(
            key=gkey, label=label, sku_code=code_display,
            is_family=is_family, is_bundle=first.is_bundle,
            members=members, member_count=len(members),
            sellable_on_hand=sell, sample_on_hand=samp, total_on_hand=tot,
            in_transit=it, unit_cogs=unit_cogs,
            sellable_value=sval, sample_value=smval, total_value=tval,
            status=status, status_count=status_count, days_of_cover=cover,
        ))

    # Default order: most-urgent first, then alphabetical by label.
    groups.sort(key=lambda g: (_BADGE_SEVERITY.get(g.status, 4), (g.label or "").lower()))
    return groups
