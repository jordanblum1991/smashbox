"""One-shot: add SBX-OG-PRIMER-BUNDLE to the Bundle catalog.

Bundle tiktok_sku_id = 1729503708543358123 was sitting uncatalogued with
170 units of recent sales — counted as a single phantom SKU instead of
exploding into its components. This script creates the Bundle + 2
BundleComponent rows so bundle_component_breakdown picks it up and
demand routes to the underlying SBX-C01101 + SBX-C6R801 components.

Component_sku uses C-form (`C01101`, `C6R801`) to match the convention
of the other 21-or-so bundles already in the catalog (verified via
production query before writing this script).

Run on production via:
    fly ssh console -C 'python scripts/add_og_primer_bundle.py'

Idempotent — bails out if the bundle row already exists.
"""
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.models.bundle import Bundle, BundleComponent
from app.models.sku import Sku


BUNDLE_TIKTOK_SKU_ID = "1729503708543358123"
BUNDLE_SKU = "SBX-OG-PRIMER-BUNDLE"
BUNDLE_NAME = "OG Primer Bundle"
BRAND = "smashbox"

COMPONENT_SBX_SKUS = ("SBX-C01101", "SBX-C6R801")


def _canonical_sku_row(db, sbx_code: str) -> Sku:
    """Pick the canonical Sku row for a given SBX-form code — the one we've
    been treating as the canonical TikTok ID in the alias map. There may be
    multiple Sku rows for the same SBX code (one per variation / re-listing);
    we want the most-recent / current one."""
    rows = db.execute(
        select(Sku).where(Sku.sku == sbx_code).order_by(Sku.id.desc())
    ).scalars().all()
    if not rows:
        raise RuntimeError(f"No catalog row for {sbx_code}")
    return rows[0]


def main() -> int:
    with SessionLocal() as db:
        existing = db.execute(
            select(Bundle).where(Bundle.tiktok_sku_id == BUNDLE_TIKTOK_SKU_ID)
        ).scalar_one_or_none()
        if existing is not None:
            print(f"Bundle already exists (id={existing.id}); aborting.")
            return 1

        components_info = [(sbx, _canonical_sku_row(db, sbx))
                           for sbx in COMPONENT_SBX_SKUS]

        bundle_msrp = sum(
            (s.msrp or Decimal("0")) for _, s in components_info
        )

        bundle = Bundle(
            bundle_sku=BUNDLE_SKU,
            tiktok_sku_id=BUNDLE_TIKTOK_SKU_ID,
            name=BUNDLE_NAME,
            brand=BRAND,
            msrp=bundle_msrp,
            selling_price=Decimal("0"),
        )
        db.add(bundle)
        db.flush()

        for _, sku in components_info:
            # Use C-form (`tiktok_alt_sku`) for component_sku — matches the
            # convention of every other bundle in the catalog. Fall back to
            # SBX-form only if the catalog row genuinely has no C-form.
            component_sku = sku.tiktok_alt_sku or sku.sku
            db.add(BundleComponent(
                bundle_id=bundle.id,
                component_sku=component_sku,
                component_name=sku.name,
                quantity=1,
                msrp=sku.msrp or Decimal("0"),
                unit_cogs=sku.unit_cogs or Decimal("0"),
            ))
            print(f"  + 1x {component_sku} ({sku.name[:50]}) "
                  f"msrp=${sku.msrp} cogs=${sku.unit_cogs}")

        db.commit()
        print(f"Created bundle id={bundle.id} {BUNDLE_SKU} = "
              f"tiktok_sku_id {BUNDLE_TIKTOK_SKU_ID}")
        print(f"  bundle MSRP: ${bundle.msrp}")
        print(f"  bundle calculated_cogs: ${bundle.calculated_cogs}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
