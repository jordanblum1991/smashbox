"""Resolve OrderLine.sku against the SKU master and bundle catalog.

Canonical identifier
--------------------
TikTok SKU ID is the canonical product identifier — it is the only key that
TikTok always emits, and it uniquely identifies a SKU/bundle in TikTok's
system. The orders importer already prefers the TikTok SKU ID when building
OrderLine.sku; this resolver makes the canonicalization deterministic for any
line that arrived with a different identifier (SBX-form, ALT C-form, etc.).

On match:
  - OrderLine.sku is rewritten to the TikTok SKU ID (when known).
  - OrderLine.unit_cogs_snapshot is captured (single: Sku.unit_cogs; bundle:
    sum of component qty × unit_cogs).

The resolver runs after TIKTOK_ORDERS, SKU_MASTER, and BUNDLE_MAPPING imports
so the catalog can land before or after the orders and still back-fill them.
Idempotent.
"""
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.bundle import Bundle
from app.models.order import OrderLine
from app.models.sku import Sku


@dataclass
class ResolveStats:
    lines_inspected: int = 0
    lines_resolved_sku: int = 0
    lines_resolved_bundle: int = 0
    lines_unresolved: int = 0


def resolve_all_order_lines(db: Session) -> ResolveStats:
    stats = ResolveStats()

    skus = db.execute(select(Sku)).scalars().all()
    bundles = db.execute(select(Bundle)).scalars().all()

    # Build a lookup keyed by ANY known identifier of a SKU.
    sku_by_key: dict[str, Sku] = {}
    for s in skus:
        for key in (s.tiktok_sku_id, s.sku, s.tiktok_alt_sku):
            if key:
                sku_by_key[str(key).strip()] = s

    bundle_by_key: dict[str, Bundle] = {}
    for b in bundles:
        for key in (b.tiktok_sku_id, b.bundle_sku):
            if key:
                bundle_by_key[str(key).strip()] = b

    for line in db.execute(select(OrderLine)).scalars():
        stats.lines_inspected += 1
        raw = (line.sku or "").strip()
        if not raw:
            stats.lines_unresolved += 1
            continue

        sku = sku_by_key.get(raw)
        if sku:
            # Canonicalize to TikTok SKU ID when the master row has one;
            # otherwise leave OrderLine.sku as the matched key (so the join
            # by Sku.sku still works for SKUs not yet on TikTok).
            line.sku = sku.tiktok_sku_id or sku.sku
            line.unit_cogs_snapshot = sku.unit_cogs
            stats.lines_resolved_sku += 1
            continue

        bundle = bundle_by_key.get(raw)
        if bundle:
            line.sku = bundle.tiktok_sku_id or bundle.bundle_sku
            line.unit_cogs_snapshot = bundle.calculated_cogs
            stats.lines_resolved_bundle += 1
            continue

        stats.lines_unresolved += 1

    return stats
