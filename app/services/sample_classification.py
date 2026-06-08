"""Single source of truth for classifying orders as samples.

TikTok's settlement file marks sample orders via the `Sample order type` column
("free sample from seller", "free sample from Tiktok Shop", "paid sample",
"oversample"). Samples must NOT count as PAID GMV — TikTok excludes them and the
buyer paid $0. The orders-file gross heuristic ($0 → SAMPLE) misses
"free sample from Tiktok Shop" orders, which carry a nominal product gross
(e.g. $46), so we reconcile classification from settlement HERE — in one place,
idempotently, independent of which file was imported first.

This replaces the per-importer inline promotion that only fired when the
settlement was imported after its order, which let later-arriving order rows
stay misclassified as PAID (a real GMV discrepancy).
"""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.order import Order, OrderType
from app.models.settlement import Settlement


def classify_from_sample_order_type(sot: str | None) -> OrderType | None:
    """Map a settlement `Sample order type` value to an OrderType.

    Returns None when the value isn't a sample flag — callers leave the
    existing order_type untouched (classification only ever promotes toward a
    sample type; it never demotes a sample back to PAID)."""
    s = (sot or "").strip().lower()
    if "free sample" in s:
        return OrderType.SAMPLE
    if "paid sample" in s or "oversample" in s:
        return OrderType.PAID_SAMPLE
    return None


def reconcile_sample_classifications(db: Session) -> int:
    """Apply each order's latest-settlement sample classification to
    `Order.order_type`. Promotes PAID → SAMPLE / PAID_SAMPLE when the settlement
    says so; never demotes (a settlement with no sample flag leaves the order
    untouched). Idempotent. Returns the number of orders whose type changed.

    Caller commits.
    """
    # Latest sample_order_type per order — "free/paid sample" is a classification,
    # not an aggregate, so the most recent settlement (by paid then settled date)
    # wins, matching the prior inline behaviour.
    best: dict[str, tuple[datetime, str | None]] = {}
    for oid, sot, paid, settled in db.execute(select(
        Settlement.tiktok_order_id, Settlement.sample_order_type,
        Settlement.paid_date, Settlement.settled_date,
    )).all():
        key = paid or settled or datetime.min
        if oid not in best or key > best[oid][0]:
            best[oid] = (key, sot)

    changed = 0
    for oid, (_, sot) in best.items():
        want = classify_from_sample_order_type(sot)
        if want is None:
            continue
        order = db.execute(
            select(Order).where(Order.tiktok_order_id == oid)
        ).scalar_one_or_none()
        if order is not None and order.order_type != want:
            order.order_type = want
            changed += 1
    return changed
