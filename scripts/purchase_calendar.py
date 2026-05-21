"""Forward-looking purchase calendar.

The planner's main table answers "what needs to be ordered RIGHT NOW".
This script answers the broader question: for every reorderable SKU,
when's the NEXT PO due and how big should it be?

For each SKU we compute:
  - days until on_hand crosses the reorder point at current velocity
  - projected order-by date (= reorder-point crossing date, since the
    next PO must be placed when on_hand hits the reorder point so that
    it arrives `lead_time` days later, before stockout)
  - projected PO quantity = velocity × (lead_time + cover_days) − on_hand_at_crossing
    (which simplifies to roughly velocity × cover_days for a SKU caught
    at exactly the reorder point)

Sorted by order-by date ascending. Excludes:
  - DISCONTINUED (not reorderable)
  - NO_VELOCITY (no signal to project from)

For SKUs currently AT or BELOW reorder point, "order by" is set to today
or earlier — the planner already wants you to act on these.
"""
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.reports.demand_planning import compute_demand_planning_view  # noqa: E402
from app.services.demand.replenishment import ReplenishmentStatus  # noqa: E402


def main() -> int:
    today = datetime.now().date()

    with SessionLocal() as db:
        view = compute_demand_planning_view(db)
        cover_days = view.cover_days

    plan = []
    for r in view.rows:
        # Skip rows we can't or won't act on.
        if r.status in (ReplenishmentStatus.DISCONTINUED,
                        ReplenishmentStatus.NO_VELOCITY):
            continue
        if r.daily_velocity <= 0:
            continue

        v = r.daily_velocity                       # ROBUST daily rate
        reorder_pt = Decimal(r.reorder_point)
        on_hand = Decimal(r.on_hand + r.expected_receipts)
        lead_time = r.lead_time_days

        # When does on_hand cross the reorder point?
        if on_hand <= reorder_pt:
            # Already at or below — order today (or technically yesterday;
            # the planner has been flagging this all along).
            days_until_reorder = Decimal("0")
        else:
            days_until_reorder = ((on_hand - reorder_pt) / v).quantize(Decimal("0.1"))

        order_by_date = today + timedelta(days=int(days_until_reorder))

        # PO quantity. For SKUs currently below reorder point, the planner's
        # suggested_qty already reflects the right number (MOQ + case-pack
        # adjusted). For SKUs projected to cross reorder point later, we
        # project the quantity assuming they hit exactly reorder_point at
        # the crossing date.
        if r.suggested_order_qty > 0:
            qty = r.suggested_order_qty
        else:
            # Future PO: target = velocity × (lead + cover), starting on_hand
            # = reorder_point at crossing time, no in-transit.
            target = v * Decimal(lead_time + cover_days)
            qty_d = (target - reorder_pt).to_integral_value(rounding="ROUND_HALF_UP")
            qty = max(int(qty_d), 0)

        unit_cogs = (r.investment / Decimal(r.suggested_order_qty)
                     if r.suggested_order_qty > 0 else Decimal("0"))
        investment = Decimal(qty) * unit_cogs if unit_cogs > 0 else Decimal("0")

        plan.append({
            "sku": r.sku_code or r.component_sku,
            "name": (r.name or "").strip(),
            "status": r.status.value,
            "on_hand": r.on_hand,
            "in_transit": r.expected_receipts,
            "velocity": v,
            "lead": lead_time,
            "reorder_pt": r.reorder_point,
            "days_until": int(days_until_reorder),
            "order_by": order_by_date,
            "qty": qty,
            "investment": investment,
        })

    plan.sort(key=lambda p: (p["order_by"], -float(p["investment"])))

    # Bucket by urgency for readable output.
    overdue_today = [p for p in plan if p["order_by"] <= today]
    next_30 = [p for p in plan if today < p["order_by"] <= today + timedelta(days=30)]
    next_60 = [p for p in plan if today + timedelta(days=30) < p["order_by"] <= today + timedelta(days=60)]
    next_90 = [p for p in plan if today + timedelta(days=60) < p["order_by"] <= today + timedelta(days=90)]
    later = [p for p in plan if p["order_by"] > today + timedelta(days=90)]

    print(f"Purchase calendar  (as of {today}, cover_days={cover_days})")
    print(f"Total reorderable SKUs in plan: {len(plan)}")
    print()

    def _bucket(label: str, rows: list, show_full: bool = True):
        if not rows:
            return
        total_inv = sum((r["investment"] for r in rows), Decimal("0"))
        print(f"━━ {label}  ({len(rows)} SKU{'s' if len(rows) != 1 else ''}, "
              f"investment ${total_inv:,.2f})")
        print(f"  {'order_by':10}  {'sku':18}  {'status':14}  {'on_hand':>7}  "
              f"{'velocity':>8}  {'lead':>5}  {'qty':>5}  {'investment':>11}  product")
        print(f"  {'-'*10}  {'-'*18}  {'-'*14}  {'-'*7}  {'-'*8}  {'-'*5}  "
              f"{'-'*5}  {'-'*11}  -------")
        for r in rows:
            print(f"  {str(r['order_by']):10}  {r['sku'][:18]:18}  "
                  f"{r['status']:14}  {r['on_hand']:>7}  {r['velocity']:>8}  "
                  f"{r['lead']:>4}d  {r['qty']:>5}  ${r['investment']:>10,.2f}  "
                  f"{r['name'][:40]}")
        print()

    _bucket("ORDER NOW (overdue or due today)", overdue_today)
    _bucket("ORDER WITHIN 30 DAYS", next_30)
    _bucket("ORDER WITHIN 60 DAYS", next_60)
    _bucket("ORDER WITHIN 90 DAYS", next_90)
    _bucket(f"ORDER 90+ DAYS OUT (overstocked, will cross eventually)", later)

    total_inv = sum((p["investment"] for p in plan), Decimal("0"))
    print(f"Total projected purchase investment across all buckets: ${total_inv:,.2f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
