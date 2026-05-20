"""Bundle → component-units expansion.

TikTok orders sometimes carry a bundle SKU ID instead of the underlying
components. For inventory math (velocity, on-hand, days-of-cover) we need
to translate "1 unit of Bundle X sold" into "1 of Component A + 1 of
Component B + …".

Extracted from `app/reports/sample_tracking.count_sku_units_shipped` so the
demand planner can use the same logic on paid sales. The function there
delegates to `expanded_units_for_sku_groups` here.

Conventions:
- The caller hands in a mapping of `{sku_key: units}` where sku_key is
  whatever identifier the source row carries (TikTok SKU ID, SBX-form, or
  bundle SKU). We don't care which form.
- For each key, we look up a Bundle row whose `tiktok_sku_id` OR
  `bundle_sku` matches. If found, multiply by the sum of component
  quantities. If not, the key is a single SKU — multiplier stays 1.
- Returns total expanded units. Bundle components are NOT separately
  enumerated here — the planner reads them per-component in its own pass
  (see `velocity.py`).
"""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.bundle import Bundle, BundleComponent


def bundle_multipliers(db: Session, sku_keys: set[str]) -> dict[str, int]:
    """Return `{sku_key: bundle_size}` for any sku_key that resolves to a
    Bundle. SKUs that don't match a bundle are simply absent — caller treats
    those as multiplier=1.
    """
    if not sku_keys:
        return {}

    bundles = db.execute(
        select(Bundle).where(
            (Bundle.tiktok_sku_id.in_(sku_keys)) | (Bundle.bundle_sku.in_(sku_keys))
        )
    ).scalars().all()
    if not bundles:
        return {}

    component_totals = dict(
        db.execute(
            select(
                BundleComponent.bundle_id,
                func.coalesce(func.sum(BundleComponent.quantity), 0),
            )
            .where(BundleComponent.bundle_id.in_([b.id for b in bundles]))
            .group_by(BundleComponent.bundle_id)
        ).all()
    )

    out: dict[str, int] = {}
    for b in bundles:
        # A bundle with no components in the DB shouldn't double-count its parent SKU
        # as if it were a single unit (we'd over-expand). Default-to-1 keeps that safe.
        multiplier = int(component_totals.get(b.id, 0)) or 1
        for key in (b.tiktok_sku_id, b.bundle_sku):
            if key:
                out[str(key)] = multiplier
    return out


def expanded_units_for_sku_groups(
    db: Session, units_by_sku: dict[str, int]
) -> int:
    """Total physical units across all SKU groups, with bundles expanded.

    `units_by_sku` is `{sku_key: order_units}`. Bundle keys get multiplied
    by their component sum; everything else passes through.
    """
    if not units_by_sku:
        return 0
    multipliers = bundle_multipliers(db, set(units_by_sku))
    return sum(units * multipliers.get(key, 1) for key, units in units_by_sku.items())


def bundle_component_breakdown(
    db: Session, sku_keys: set[str]
) -> dict[str, list[tuple[str, int]]]:
    """For each bundle key, return a list of `(component_sku, qty_per_bundle)`.

    Used by the demand planner to map a bundle sale onto per-component
    velocity ("1 bundle sold drives 1 unit of demand for each of components
    A, B, C"). Non-bundle keys are absent from the result.
    """
    if not sku_keys:
        return {}
    bundles = db.execute(
        select(Bundle).where(
            (Bundle.tiktok_sku_id.in_(sku_keys)) | (Bundle.bundle_sku.in_(sku_keys))
        )
    ).scalars().all()
    if not bundles:
        return {}

    components_by_bundle: dict[int, list[tuple[str, int]]] = {}
    for c in db.execute(
        select(BundleComponent).where(
            BundleComponent.bundle_id.in_([b.id for b in bundles])
        )
    ).scalars():
        components_by_bundle.setdefault(c.bundle_id, []).append(
            (c.component_sku, int(c.quantity or 0))
        )

    out: dict[str, list[tuple[str, int]]] = {}
    for b in bundles:
        comps = components_by_bundle.get(b.id, [])
        if not comps:
            continue
        for key in (b.tiktok_sku_id, b.bundle_sku):
            if key:
                out[str(key)] = comps
    return out
