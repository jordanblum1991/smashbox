"""Resolve OrderLine.sku against the SKU master and bundle catalog.

Why this exists
---------------
The TikTok orders file may emit any of three identifiers for a single SKU
(SBX-form, ALT C-form, or numeric TikTok SKU ID), and for bundles it emits the
bundle's TikTok SKU ID. The resolver:

  1. Rewrites OrderLine.sku to the canonical form whenever a match is found.
  2. Writes OrderLine.unit_cogs_snapshot — either from Sku.unit_cogs (single
     items) or from the sum of component COGS (bundles). The snapshot is
     captured at resolution time so historical reports don't drift if the
     master is later edited.

Idempotent. Safe to run multiple times — and the orders importer / SKU master
importer both trigger it at the end of their batch so the catalog stays in
sync with as-imported data.
"""
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import or_, select
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
    """Walk every OrderLine and try to resolve its sku against catalog tables."""
    stats = ResolveStats()

    # Cache the catalog once — there are at most a few hundred rows of each.
    skus = db.execute(select(Sku)).scalars().all()
    bundles = db.execute(select(Bundle)).scalars().all()

    sku_by_key: dict[str, Sku] = {}
    for s in skus:
        for key in (s.sku, s.tiktok_alt_sku, s.tiktok_sku_id):
            if key:
                sku_by_key[str(key).strip()] = s

    bundle_by_key: dict[str, Bundle] = {}
    for b in bundles:
        for key in (b.bundle_sku, b.tiktok_sku_id):
            if key:
                bundle_by_key[str(key).strip()] = b

    for line in db.execute(select(OrderLine)).scalars():
        stats.lines_inspected += 1
        raw = (line.sku or "").strip()
        if not raw:
            stats.lines_unresolved += 1
            continue

        # Single-SKU match first.
        sku = sku_by_key.get(raw)
        if sku:
            line.sku = sku.sku  # canonicalize
            line.unit_cogs_snapshot = sku.unit_cogs
            stats.lines_resolved_sku += 1
            continue

        # Bundle match — sum component COGS for the unit COGS snapshot.
        bundle = bundle_by_key.get(raw)
        if bundle:
            if bundle.bundle_sku:
                line.sku = bundle.bundle_sku
            line.unit_cogs_snapshot = bundle.calculated_cogs
            stats.lines_resolved_bundle += 1
            continue

        stats.lines_unresolved += 1

    return stats
