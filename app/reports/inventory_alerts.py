"""Low-stock / reorder alert summary — the count behind the nav badge and the
dashboard banner.

The reorder status (out-of-stock / at-risk / reorder-now) comes from the demand
planner, which computes sales velocity over the order history — too heavy to run
on every request. So the per-request callers read a TTL-cached summary
(`get_inventory_alert_summary`); the full list still lives on the Demand Planning
page. Single uvicorn process + 2 users → a process-global cache is safe and a
few-minutes-stale badge is fine.
"""
from __future__ import annotations

import time

from sqlalchemy.orm import Session

from app.reports.demand_planning import compute_demand_planning_view

# Statuses that mean "act now" — surfaced in the badge/banner count.
_ACTIONABLE = ("out_of_stock", "at_risk", "reorder_now")

_TTL_SECONDS = 600  # 10 min
_cache: dict = {"at": float("-inf"), "data": None}


def compute_inventory_alert_summary(db: Session) -> dict:
    """{count, out_of_stock, at_risk, reorder_now} for sellable SKUs needing
    attention, from the demand planner's per-SKU status."""
    counts = compute_demand_planning_view(db).counts_by_status
    out_of_stock = counts.get("out_of_stock", 0)
    at_risk = counts.get("at_risk", 0)
    reorder_now = counts.get("reorder_now", 0)
    return {
        "count": out_of_stock + at_risk + reorder_now,
        "out_of_stock": out_of_stock,
        "at_risk": at_risk,
        "reorder_now": reorder_now,
    }


def get_inventory_alert_summary(db: Session, *, ttl: float = _TTL_SECONDS) -> dict:
    """Cached wrapper for per-request callers (nav badge, dashboard banner).
    Recomputes at most once per `ttl` seconds; otherwise returns the last result.
    Cheap on a cache hit; the heavy velocity compute runs ~once every 10 min."""
    now = time.monotonic()
    if _cache["data"] is None or (now - _cache["at"]) > ttl:
        _cache["data"] = compute_inventory_alert_summary(db)
        _cache["at"] = now
    return _cache["data"]


def _reset_cache() -> None:
    """Test hook — force the next call to recompute."""
    _cache["data"] = None
    _cache["at"] = float("-inf")
