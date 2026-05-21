"""Read-only audit of SkuAlias mappings.

Pulls every registered alias and, for both sides, resolves:
  - product title (from Sku catalog; falls back to Bundle catalog)
  - total PAID Shipped/Completed units in the last 60 days
  - current latest on_hand from InventorySnapshot

Then flags three "wrong merge" patterns:
  Flag 1: product titles diverge between alias and canonical
          (different Sku.sku codes, or one side has no catalog row at all)
  Flag 2: a canonical accumulates more than 2 distinct HUMAN-READABLE
          (non-numeric) alias source codes — pattern-match may have
          over-grouped variations into one canonical
  Flag 3: BOTH sides had non-zero PAID sales in the last 30 days —
          real re-listing duplicates usually have sales on one side
          (the new one) while the other side has gone dormant; both
          live suggests two distinct products got fused

Does NOT modify anything. Strictly read-only.
"""
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, or_, select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models.bundle import Bundle  # noqa: E402
from app.models.inventory_snapshot import InventorySnapshot  # noqa: E402
from app.models.order import Order, OrderLine, OrderType  # noqa: E402
from app.models.sku import Sku  # noqa: E402
from app.models.sku_alias import SkuAlias  # noqa: E402


def _resolve_catalog(db, code: str) -> tuple[str, str | None]:
    """Find a label + the Sku.sku code (if any) for a SKU identifier.

    Returns (label, underlying_sku_code). `underlying_sku_code` is the
    Sku.sku field, used to test whether two sides refer to the same
    physical product. Returns None when no catalog row exists."""
    s = db.execute(
        select(Sku).where(
            or_(Sku.tiktok_sku_id == code, Sku.sku == code, Sku.tiktok_alt_sku == code)
        )
    ).scalars().first()
    if s:
        return (f"{s.sku} — {(s.name or '').strip()[:60]}", s.sku)

    b = db.execute(
        select(Bundle).where(or_(Bundle.tiktok_sku_id == code, Bundle.bundle_sku == code))
    ).scalars().first()
    if b:
        return (f"[BUNDLE] {b.bundle_sku or '?'} — {(b.name or '').strip()[:60]}",
                b.bundle_sku)

    return ("(no catalog row)", None)


def _units_in_window(db, code: str, days: int) -> int:
    """Total PAID Shipped/Completed units for this raw SKU code over the
    trailing `days` window. Does NOT apply aliases — we want the raw
    pre-merge count for each side."""
    cutoff = datetime.now() - timedelta(days=days)
    n = db.execute(
        select(func.coalesce(func.sum(OrderLine.quantity), 0))
        .join(Order, Order.id == OrderLine.order_id)
        .where(OrderLine.sku == code)
        .where(Order.placed_at >= cutoff)
        .where(Order.order_type.in_([OrderType.PAID, OrderType.PAID_SAMPLE]))
        .where(Order.status.in_(["Shipped", "Completed"]))
    ).scalar()
    return int(n or 0)


def _latest_on_hand(db, code: str) -> int:
    snap = db.execute(
        select(InventorySnapshot)
        .where(InventorySnapshot.sku == code)
        .order_by(InventorySnapshot.captured_at.desc())
        .limit(1)
    ).scalars().first()
    return int(snap.on_hand) if snap else 0


def _is_human_readable(code: str) -> bool:
    """A TikTok numeric ID is 16-19 digits; everything else is human-readable."""
    return not (code.isdigit() and len(code) >= 16)


def main() -> int:
    with SessionLocal() as db:
        aliases = db.execute(select(SkuAlias).order_by(SkuAlias.id)).scalars().all()

        rows = []
        for a in aliases:
            alias_label, alias_sku = _resolve_catalog(db, a.alias_sku)
            canon_label, canon_sku = _resolve_catalog(db, a.canonical_sku)
            rows.append({
                "id": a.id,
                "alias": a.alias_sku,
                "canonical": a.canonical_sku,
                "alias_label": alias_label,
                "canon_label": canon_label,
                "alias_sku": alias_sku,
                "canon_sku": canon_sku,
                "alias_60d": _units_in_window(db, a.alias_sku, 60),
                "canon_60d": _units_in_window(db, a.canonical_sku, 60),
                "alias_30d": _units_in_window(db, a.alias_sku, 30),
                "canon_30d": _units_in_window(db, a.canonical_sku, 30),
                "alias_oh": _latest_on_hand(db, a.alias_sku),
                "canon_oh": _latest_on_hand(db, a.canonical_sku),
                "notes": a.notes or "",
            })

        # ---- Full table -----------------------------------------------------
        print(f"\nTotal registered aliases: {len(rows)}")
        print()
        for r in rows:
            print(f"#{r['id']:>3}  {r['alias'][:22]:22} -> {r['canonical'][:22]:22}")
            print(f"      A: {r['alias_label']}")
            print(f"      C: {r['canon_label']}")
            print(f"      60d sales:  alias={r['alias_60d']:>4}   canon={r['canon_60d']:>5}")
            print(f"      on_hand:    alias={r['alias_oh']:>4}   canon={r['canon_oh']:>5}")
            if r["notes"]:
                print(f"      note: {r['notes']}")
            print()

        # ---- Flag 1: product title mismatch --------------------------------
        print("=" * 72)
        print("FLAG 1 — alias and canonical resolve to different products")
        print("=" * 72)
        flag1 = []
        for r in rows:
            # Mismatch when both sides have catalog rows AND their Sku.sku
            # codes differ. Missing catalog rows = can't compare = skip.
            if r["alias_sku"] and r["canon_sku"] and r["alias_sku"] != r["canon_sku"]:
                flag1.append(r)
            elif r["alias_sku"] is None or r["canon_sku"] is None:
                # one or both have no catalog row — flag separately as
                # "can't confirm match" (less severe but worth surfacing).
                pass
        if not flag1:
            print("  (none)")
        for r in flag1:
            print(f"  #{r['id']}  {r['alias']} -> {r['canonical']}")
            print(f"    A: {r['alias_label']}")
            print(f"    C: {r['canon_label']}")
            print()

        # Sub-flag 1b: missing catalog row on either side
        missing = [r for r in rows if r["alias_sku"] is None or r["canon_sku"] is None]
        if missing:
            print()
            print("  Pairs with at least one side missing a catalog row "
                  "(can't confirm match):")
            for r in missing:
                a_status = "ok" if r["alias_sku"] else "MISSING"
                c_status = "ok" if r["canon_sku"] else "MISSING"
                print(f"    #{r['id']}  alias={a_status} canonical={c_status}  "
                      f"{r['alias']} -> {r['canonical']}")

        # ---- Flag 2: canonical with >2 human-readable aliases --------------
        print()
        print("=" * 72)
        print("FLAG 2 — canonical with more than 2 distinct human-readable aliases")
        print("=" * 72)
        human_aliases_by_canonical: dict[str, list[str]] = defaultdict(list)
        for r in rows:
            if _is_human_readable(r["alias"]):
                human_aliases_by_canonical[r["canonical"]].append(r["alias"])
        flag2 = {k: v for k, v in human_aliases_by_canonical.items() if len(v) > 2}
        if not flag2:
            print("  (none)")
        for canonical, alist in flag2.items():
            label, _ = _resolve_catalog(db, canonical)
            print(f"  canonical {canonical}  ({label})")
            for a in alist:
                print(f"    <- {a}")
            print()

        # ---- Flag 3: both sides had sales in last 30 days ------------------
        print()
        print("=" * 72)
        print("FLAG 3 — both sides had PAID sales in the last 30 days")
        print("=" * 72)
        flag3 = [r for r in rows if r["alias_30d"] > 0 and r["canon_30d"] > 0]
        if not flag3:
            print("  (none)")
        for r in flag3:
            print(f"  #{r['id']}  {r['alias']} -> {r['canonical']}")
            print(f"    A: {r['alias_label']}  (30d sales: {r['alias_30d']})")
            print(f"    C: {r['canon_label']}  (30d sales: {r['canon_30d']})")
            print()

        # ---- Summary -------------------------------------------------------
        print()
        print("=" * 72)
        print("SUMMARY")
        print("=" * 72)
        print(f"  Total aliases:                                       {len(rows)}")
        print(f"  Flag 1 (product mismatch — different Sku.sku):       {len(flag1)}")
        print(f"  Flag 1b (one side missing catalog row):              {len(missing)}")
        print(f"  Flag 2 (canonical with >2 human-readable aliases):   {len(flag2)}")
        print(f"  Flag 3 (both sides selling in last 30d):             {len(flag3)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
