"""Read-only: count how many SKUs use the variance vs flat safety-stock method.

The 'Safety stock' dropdown on the planner only affects the flat-method
safety_stock_pct. SKUs in the variance branch ignore it entirely.
"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.reports.demand_planning import compute_demand_planning_view  # noqa: E402


def main() -> int:
    with SessionLocal() as db:
        view = compute_demand_planning_view(db)
    counts = Counter(r.safety_method for r in view.rows)
    print(f"Total SKUs in planner: {len(view.rows)}")
    print()
    print(f"Safety stock method distribution:")
    print(f"  variance (z × σ × √L)              {counts.get('variance', 0):>3}   "
          f"← dropdown is IGNORED for these")
    print(f"  flat     (lead_demand × pct)       {counts.get('flat', 0):>3}   "
          f"← dropdown ONLY affects these")
    print()
    flat_skus = [r for r in view.rows if r.safety_method == "flat"]
    if flat_skus:
        print(f"SKUs using flat method:")
        for r in flat_skus:
            print(f"  {(r.sku_code or r.component_sku)[:18]:18}  "
                  f"status={r.status.value:14}  velocity={r.daily_velocity}  "
                  f"safety_stock_units={r.safety_stock_units}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
