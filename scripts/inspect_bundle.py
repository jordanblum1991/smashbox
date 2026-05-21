"""Dump a Bundle row + its components by tiktok_sku_id.

Usage:  python scripts/inspect_bundle.py <tiktok_sku_id>
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models.bundle import Bundle, BundleComponent  # noqa: E402


def main(tiktok_sku_id: str) -> int:
    with SessionLocal() as db:
        b = db.execute(
            select(Bundle).where(Bundle.tiktok_sku_id == tiktok_sku_id)
        ).scalar_one_or_none()
        if b is None:
            print(f"No bundle with tiktok_sku_id={tiktok_sku_id}")
            return 1
        print(f"Bundle: id={b.id}")
        print(f"  bundle_sku:     {b.bundle_sku}")
        print(f"  tiktok_sku_id:  {b.tiktok_sku_id}")
        print(f"  name:           {b.name}")
        print(f"  brand:          {b.brand}")
        print(f"  msrp:           {b.msrp}")
        print(f"  selling_price:  {b.selling_price}")
        print(f"  calculated_cogs:{b.calculated_cogs}")
        comps = db.execute(
            select(BundleComponent).where(BundleComponent.bundle_id == b.id)
        ).scalars().all()
        print(f"  components ({len(comps)}):")
        for c in comps:
            print(f"    component_sku={c.component_sku!r:20} qty={c.quantity}  "
                  f"msrp={c.msrp}  unit_cogs={c.unit_cogs}  name={c.component_name!r}")
        return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/inspect_bundle.py <tiktok_sku_id>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
