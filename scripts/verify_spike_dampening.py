"""One-shot: verify spike-dampened velocity is actually changing suggested qty.

For every SKU with sales, computes:
  raw_qty      = target qty using the uncapped 60-day mean
  robust_qty   = target qty using the spike-dampened mean (the prod path)
  delta        = raw_qty - robust_qty (positive = dampening shrunk the order)

If the prod planner is using spike-dampened velocity correctly, we expect
at least some SKUs to show delta > 0 (their raw daily mean had outliers
that got clipped). Reports anything with a meaningful delta.
"""
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.services.demand.velocity import compute_velocity  # noqa: E402


COVER_DAYS = 45
LEAD_TIME_DAYS = 14


def _qty_with_velocity(daily_velocity: Decimal) -> int:
    """How many units the order-quantity math would target at a given daily
    rate, for a SKU at on_hand=0 (typical for our dataset). Excludes
    safety stock — that doesn't enter the qty formula in compute_one."""
    target = daily_velocity * Decimal(LEAD_TIME_DAYS + COVER_DAYS)
    return int(target.to_integral_value(rounding="ROUND_HALF_UP"))


def main() -> int:
    with SessionLocal() as db:
        velocities = compute_velocity(db, as_of=datetime.now())

    diffs = []
    same = 0
    for sku, v in velocities.items():
        raw_qty = _qty_with_velocity(v.daily_60d_raw)
        robust_qty = _qty_with_velocity(v.daily_60d_robust)
        delta = raw_qty - robust_qty
        if delta != 0:
            diffs.append((sku, v.daily_60d_raw, v.daily_60d_robust, raw_qty, robust_qty, delta))
        else:
            same += 1

    diffs.sort(key=lambda d: d[5], reverse=True)

    print(f"Total SKUs with sales:              {len(velocities)}")
    print(f"SKUs where dampening changes qty:   {len(diffs)}")
    print(f"SKUs where qty is unchanged:        {same}")
    print()
    if not diffs:
        print("  (no diff — either no outlier days, or dampening not in effect)")
    else:
        print(f"  {'sku':22} {'raw':>8} {'robust':>8} {'raw_qty':>9} {'robust_qty':>11} {'savings':>9}")
        for sku, raw, robust, rq, robq, delta in diffs[:15]:
            print(f"  {sku[:22]:22} {raw:>8} {robust:>8} {rq:>9} {robq:>11} {delta:>+9}")
        print()
        total_savings = sum(d[5] for d in diffs)
        print(f"  Aggregate qty reduction from dampening across all SKUs: {total_savings} units")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
