"""One-shot: list any SKU codes that appear more than once in the planner.

After alias merging, every physical product should be one row in the
planner. A `sku_code` that shows up under multiple `component_sku` keys
indicates an unmerged identifier — a candidate for another alias.

Outputs:
  - Groups of rows sharing a sku_code (the human SBX-form).
  - For each row: component_sku, on_hand, suggested_qty, daily velocity.
  - "Unmapped" rows (no sku_code) bucketed separately since they share
    a NULL sku_code but aren't actually the same product.
"""
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.reports.demand_planning import compute_demand_planning_view  # noqa: E402


def main() -> int:
    with SessionLocal() as db:
        view = compute_demand_planning_view(db)

    by_code: dict[str, list] = defaultdict(list)
    unmapped: list = []
    for r in view.rows:
        if r.sku_code:
            by_code[r.sku_code].append(r)
        else:
            unmapped.append(r)

    duplicates = {code: rows for code, rows in by_code.items() if len(rows) > 1}

    print(f"Total planner rows:           {len(view.rows)}")
    print(f"Distinct sku_codes:           {len(by_code)}")
    print(f"Duplicate sku_codes:          {len(duplicates)}")
    print(f"Unmapped rows (no sku_code):  {len(unmapped)}")
    print()

    if not duplicates:
        print("  No duplicates. Every sku_code appears exactly once.")
    else:
        print("Duplicates:")
        print()
        for code, rows in sorted(duplicates.items()):
            print(f"  {code}  ({len(rows)} rows)")
            for r in rows:
                print(f"    component_sku={r.component_sku:24}  "
                      f"on_hand={r.on_hand:>4}  "
                      f"sugg_qty={r.suggested_order_qty:>4}  "
                      f"daily_v={r.daily_velocity}  "
                      f"status={r.status.value}")
            print()

    if unmapped:
        print()
        print("Unmapped rows (no Sku catalog row matched — separate from duplicates):")
        for r in unmapped:
            print(f"  component_sku={r.component_sku:24}  "
                  f"on_hand={r.on_hand:>4}  sugg_qty={r.suggested_order_qty:>4}  "
                  f"daily_v={r.daily_velocity}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
