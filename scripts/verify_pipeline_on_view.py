"""Read-only: verify DemandPlanningView.pipeline is populated on prod.

Computes the planner view and dumps the pipeline bucket structure,
proving the new section will render correctly when an authenticated
user loads /reports/demand-planning.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.reports.demand_planning import compute_demand_planning_view  # noqa: E402


def main() -> int:
    with SessionLocal() as db:
        view = compute_demand_planning_view(db)

    p = view.pipeline
    print(f"Planner view computed: {len(view.rows)} rows, cover_days={view.cover_days}")
    print()
    print(f"Pipeline buckets:")
    print(f"  overdue:   {len(p.overdue):>3} SKUs   ${p.overdue_investment:>10,.2f}")
    print(f"  next_30:   {len(p.next_30):>3} SKUs   ${p.next_30_investment:>10,.2f}")
    print(f"  next_60:   {len(p.next_60):>3} SKUs   ${p.next_60_investment:>10,.2f}")
    print(f"  next_90:   {len(p.next_90):>3} SKUs   ${p.next_90_investment:>10,.2f}")
    print(f"  ─────────────────────────────────────────")
    print(f"  total:     {len(p.overdue) + len(p.next_30) + len(p.next_60) + len(p.next_90):>3} SKUs   ${p.total_investment:>10,.2f}")
    print()

    print("All pipeline items, sorted by order_by_date:")
    for item in p.all_items_sorted:
        print(f"  {str(item.order_by_date):10}  +{item.days_until_reorder:>2}d  "
              f"{(item.sku_code or '?')[:14]:14}  {item.status.value:14}  "
              f"qty={item.suggested_qty:>5}  ${item.investment:>10,.2f}")

    if not p.all_items_sorted:
        print("  (empty — no SKUs need ordering in the next 90 days)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
