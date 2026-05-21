"""Read-only: explain why cover_days isn't dramatically moving Reorder Now.

For each SKU, computes suggested_qty + investment at the default cover_days
(45) and at the buyer's elevated cover_days (120). Reports:
  - per-SKU comparison
  - total Reorder Now $ at each setting
  - status distribution at each setting
  - flag SKUs whose qty didn't scale (MOQ binding, or status-suppressed)

No DB writes.
"""
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.reports.demand_planning import compute_demand_planning_view  # noqa: E402


def _scenario(db, cover_days: int):
    return compute_demand_planning_view(db, cover_days=cover_days)


def main() -> int:
    with SessionLocal() as db:
        v45 = _scenario(db, cover_days=45)
        v120 = _scenario(db, cover_days=120)

    # Index by component_sku for side-by-side comparison.
    by_sku_45 = {r.component_sku: r for r in v45.rows}
    by_sku_120 = {r.component_sku: r for r in v120.rows}
    all_skus = set(by_sku_45) | set(by_sku_120)

    print(f"Total SKUs in planner: {len(all_skus)}")
    print()

    # Status distribution at each setting.
    def _status_dist(view):
        c = Counter(r.status.value for r in view.rows)
        return dict(c)

    print(f"Status distribution @ cover=45:")
    for status, n in sorted(_status_dist(v45).items()):
        print(f"  {status:18} {n:>4}")
    print()
    print(f"Status distribution @ cover=120:")
    for status, n in sorted(_status_dist(v120).items()):
        print(f"  {status:18} {n:>4}")
    print()

    # Reorder Now total.
    print(f"Reorder Now total @ cover=45:  ${v45.investment_total:,.2f}")
    print(f"Reorder Now total @ cover=120: ${v120.investment_total:,.2f}")
    delta = v120.investment_total - v45.investment_total
    ratio = (v120.investment_total / v45.investment_total
             if v45.investment_total > 0 else Decimal("0"))
    print(f"  Δ:                            ${delta:,.2f}  ({ratio:.2f}x)")
    print(f"  (a 2.27x ratio would be the pure-math expectation if cover scaled cleanly)")
    print()

    # Per-SKU contribution analysis: which SKUs actually contribute to
    # Reorder Now, and how their qty changes between scenarios.
    contributors = []
    for sku in all_skus:
        r45 = by_sku_45.get(sku)
        r120 = by_sku_120.get(sku)
        if r45 is None or r120 is None:
            continue
        if r45.suggested_order_qty == 0 and r120.suggested_order_qty == 0:
            continue  # Doesn't contribute either way.
        contributors.append({
            "sku": r45.sku_code or r45.component_sku,
            "status_45": r45.status.value,
            "status_120": r120.status.value,
            "velocity": r45.daily_velocity,
            "on_hand": r45.on_hand,
            "qty_45": r45.suggested_order_qty,
            "qty_120": r120.suggested_order_qty,
            "inv_45": r45.investment,
            "inv_120": r120.investment,
        })
    contributors.sort(key=lambda c: c["inv_120"], reverse=True)

    print(f"SKUs that contribute to Reorder Now at either setting: {len(contributors)}")
    print()
    print(f"  {'sku':16} {'status':14} {'velocity':>9} {'on_hand':>8} "
          f"{'qty_45':>7} {'qty_120':>8} {'qty_ratio':>9} "
          f"{'inv_45':>10} {'inv_120':>10}")
    print(f"  {'-'*16} {'-'*14} {'-'*9} {'-'*8} {'-'*7} {'-'*8} {'-'*9} "
          f"{'-'*10} {'-'*10}")
    for c in contributors:
        qty_ratio = (c["qty_120"] / c["qty_45"]) if c["qty_45"] > 0 else float("inf")
        ratio_str = f"{qty_ratio:.2f}x" if c["qty_45"] > 0 else "new"
        flag = ""
        if c["qty_45"] == c["qty_120"] and c["qty_45"] > 0:
            flag = "  ← UNCHANGED (MOQ floor or status flip?)"
        elif qty_ratio < 1.5 and c["qty_45"] > 0:
            flag = "  ← weaker than 2.27x (MOQ likely binding)"
        print(f"  {c['sku'][:16]:16} {c['status_45']:14} {c['velocity']:>9} "
              f"{c['on_hand']:>8} {c['qty_45']:>7} {c['qty_120']:>8} {ratio_str:>9} "
              f"${c['inv_45']:>9.2f} ${c['inv_120']:>9.2f}{flag}")

    print()

    # The "missing $" — how many SKUs in HEALTHY/OVERSTOCKED that would have
    # contributed if their on_hand were lower. Just a count.
    suppressed_45 = [r for r in v45.rows
                     if r.status.value in ("healthy", "overstocked")]
    print(f"SKUs in HEALTHY/OVERSTOCKED @ cover=45 "
          f"(contribute $0 to Reorder Now regardless of cover):  {len(suppressed_45)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
