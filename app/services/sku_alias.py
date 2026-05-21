"""SKU alias lookups, application, and suggestion.

Three responsibilities:

  1. `load_alias_map(db)` — fetch all SkuAlias rows and resolve chains
     (A→B, B→C becomes A→C and B→C) into a flat `{alias: canonical}` dict.

  2. `canonicalize(sku, alias_map)` — single-call rewrite. Returns the SKU
     unchanged if no alias is registered.

  3. `suggest_aliases(db, ...)` — read-only report of *candidate* alias
     pairs based on two heuristics:
     - **same-stem**: legacy code (e.g. `C09D01`) plus a prefixed variant
       (e.g. `SBX-C09D01`) appearing as separate codes.
     - **temporal handoff**: code A's sales drop to zero exactly as code
       B's begin (or close to it).
     Pairs are NOT persisted. The caller (a CLI or a future UI) reviews
     them and explicitly calls `upsert_alias` for the ones to keep.

The alias map is consumed by:
  - `app/services/demand/velocity.py` (collapse OrderLines before bundle
    expansion and daily bucketing).
  - `app/reports/demand_planning.py` (roll up the latest inventory
    snapshot per canonical SKU).
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.order import Order, OrderLine, OrderType
from app.models.sku import Sku
from app.models.sku_alias import SkuAlias


# Status filter shared with velocity — we only consider "real" demand for
# temporal-handoff detection (samples and cancellations don't tell us a
# SKU is going dormant).
DEMAND_COUNTED_STATUSES = ("Shipped", "Completed")
DEMAND_COUNTED_TYPES = (OrderType.PAID, OrderType.PAID_SAMPLE)


def load_alias_map(db: Session) -> dict[str, str]:
    """Returns `{alias_sku: terminal_canonical_sku}` for all registered
    aliases. Chains are pre-resolved so a caller never needs a second
    lookup; cycles are broken without infinite loop (terminal = wherever
    the walker stops).
    """
    rows = db.execute(
        select(SkuAlias.alias_sku, SkuAlias.canonical_sku)
    ).all()
    direct = {a: c for a, c in rows}
    if not direct:
        return {}

    resolved: dict[str, str] = {}
    for alias in direct:
        seen: set[str] = set()
        current = alias
        while current in direct and current not in seen:
            seen.add(current)
            current = direct[current]
        resolved[alias] = current
    return resolved


def canonicalize(sku: str | None, alias_map: dict[str, str]) -> str | None:
    """Return the canonical SKU code for `sku`, or `sku` unchanged when no
    alias is registered. `None` passes through."""
    if sku is None:
        return None
    return alias_map.get(sku, sku)


def upsert_alias(
    db: Session,
    *,
    alias_sku: str,
    canonical_sku: str,
    notes: str | None = None,
    created_by_user_id: int | None = None,
    shop_id: int | None = None,
) -> SkuAlias:
    """Create or update a SkuAlias row. Updating in place preserves the
    `created_at` audit field; `notes` and `canonical_sku` may change as
    the buyer refines mappings."""
    if alias_sku == canonical_sku:
        raise ValueError("alias_sku and canonical_sku must differ")

    existing = db.execute(
        select(SkuAlias).where(
            SkuAlias.alias_sku == alias_sku,
            SkuAlias.shop_id.is_(shop_id) if shop_id is None else SkuAlias.shop_id == shop_id,
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.canonical_sku = canonical_sku
        if notes is not None:
            existing.notes = notes
        if created_by_user_id is not None:
            existing.created_by_user_id = created_by_user_id
        db.flush()
        return existing

    row = SkuAlias(
        shop_id=shop_id,
        alias_sku=alias_sku,
        canonical_sku=canonical_sku,
        notes=notes,
        created_by_user_id=created_by_user_id,
    )
    db.add(row)
    db.flush()
    return row


# ---- Alias suggestion ------------------------------------------------------

@dataclass
class _SkuSalesWindow:
    """Aggregated sales summary for one SKU code across all PAID
    Shipped/Completed orders in the DB."""
    sku: str
    first_sale: date
    last_sale: date
    total_units: int
    days_with_sales: int


@dataclass
class AliasSuggestion:
    alias_sku: str
    canonical_sku: str
    reason: str             # "same_stem" | "temporal_handoff" | "both"
    confidence: str         # "high" | "medium" | "low"
    alias_last_sale: date | None
    canonical_first_sale: date | None
    alias_total_units: int
    canonical_total_units: int
    days_gap: int | None    # gap between alias's last sale and canonical's first


# Common prefixes to strip when stem-matching. Add to this list as the
# real catalog evolves.
_PREFIX_RE = re.compile(r"^(SBX-|S-|SKU-)", re.IGNORECASE)


def _stem(sku: str) -> str:
    """Strip a common prefix to expose the underlying product code.
    `SBX-C09D01` and `C09D01` both stem to `C09D01`."""
    return _PREFIX_RE.sub("", sku).upper()


def _pick_canonical_by_prefix(codes: list[str]) -> str:
    """Among codes sharing a stem, pick the one we'd prefer as canonical:
    the prefixed (SBX-) version if present, else the longest, else first
    alphabetically. The buyer can override on review."""
    sbx = [c for c in codes if c.upper().startswith("SBX-")]
    if sbx:
        return sorted(sbx)[0]
    return sorted(codes, key=lambda c: (-len(c), c))[0]


def _sales_windows(db: Session) -> dict[str, _SkuSalesWindow]:
    """One row per SKU code seen in PAID Shipped/Completed OrderLines:
    first_sale, last_sale, total_units, days_with_sales."""
    rows = db.execute(
        select(
            OrderLine.sku,
            func.min(Order.placed_at).label("first"),
            func.max(Order.placed_at).label("last"),
            func.coalesce(func.sum(OrderLine.quantity), 0).label("total"),
            func.count(func.distinct(func.date(Order.placed_at))).label("days"),
        )
        .join(Order, Order.id == OrderLine.order_id)
        .where(Order.order_type.in_(DEMAND_COUNTED_TYPES))
        .where(Order.status.in_(DEMAND_COUNTED_STATUSES))
        .group_by(OrderLine.sku)
    ).all()
    return {
        sku: _SkuSalesWindow(
            sku=sku,
            first_sale=first.date() if first else date.min,
            last_sale=last.date() if last else date.min,
            total_units=int(total or 0),
            days_with_sales=int(days or 0),
        )
        for sku, first, last, total, days in rows
    }


def suggest_aliases(
    db: Session,
    *,
    min_units: int = 5,
    max_handoff_gap_days: int = 21,
    quiet_window_days: int = 30,
) -> list[AliasSuggestion]:
    """Generate candidate alias pairs by two heuristics.

    Heuristic 1 — **same-stem**: any pair of SKUs whose codes differ only
    by a `SBX-`/`S-`/`SKU-` prefix. High confidence because the codes
    obviously refer to the same product family.

    Heuristic 2 — **temporal handoff**: a SKU `A` whose sales stopped
    `quiet_window_days+` ago AND another SKU `B` whose first sale is
    within `max_handoff_gap_days` of A's last. Medium confidence —
    coincidental handoff is possible, so the buyer reviews these.

    Both heuristics require each candidate to have at least `min_units`
    of historical sales (filter out noise).

    Already-aliased pairs are excluded from the output so reruns don't
    re-suggest mappings the buyer has already approved.
    """
    windows = _sales_windows(db)
    existing = load_alias_map(db)
    eligible = {sku: w for sku, w in windows.items() if w.total_units >= min_units}

    suggestions: dict[tuple[str, str], AliasSuggestion] = {}

    # ---- Heuristic 1: same stem ----------------------------------------
    by_stem: dict[str, list[str]] = defaultdict(list)
    for sku in eligible:
        by_stem[_stem(sku)].append(sku)

    for stem, codes in by_stem.items():
        if len(codes) < 2:
            continue
        canonical = _pick_canonical_by_prefix(codes)
        for alias in codes:
            if alias == canonical:
                continue
            if existing.get(alias) == canonical:
                continue
            a_w = eligible[alias]
            c_w = eligible[canonical]
            gap = (c_w.first_sale - a_w.last_sale).days if a_w.last_sale and c_w.first_sale else None
            suggestions[(alias, canonical)] = AliasSuggestion(
                alias_sku=alias,
                canonical_sku=canonical,
                reason="same_stem",
                confidence="high",
                alias_last_sale=a_w.last_sale,
                canonical_first_sale=c_w.first_sale,
                alias_total_units=a_w.total_units,
                canonical_total_units=c_w.total_units,
                days_gap=gap,
            )

    # ---- Heuristic 3: catalog-level same SKU code ----------------------
    # After the order-import resolver runs, OrderLines reference opaque
    # TikTok numeric IDs — the human-readable rename (C09D01 → SBX-C09D01)
    # is invisible at the order layer. But the Sku CATALOG still has it:
    # if two Sku rows share `Sku.sku` (the human-readable code) but have
    # different `tiktok_sku_id`, TikTok almost certainly re-listed the
    # product under a new ID, splitting demand across two TikTok identifiers.
    #
    # MEDIUM confidence because this also catches genuine TikTok variations
    # (e.g. one Sku.sku with separate color rows). The buyer reviews. The
    # canonical guess is "Sku row with the more recent last_sale" — the
    # newer listing is the current source of truth.
    catalog_rows = db.execute(
        select(Sku.sku, Sku.tiktok_sku_id)
        .where(Sku.tiktok_sku_id.is_not(None))
        .where(Sku.sku.is_not(None))
    ).all()
    catalog_by_sku: dict[str, list[str]] = defaultdict(list)
    for sku_code, tt_id in catalog_rows:
        catalog_by_sku[sku_code].append(str(tt_id))

    for sku_code, tt_ids in catalog_by_sku.items():
        if len(tt_ids) < 2:
            continue
        # Pick canonical = the one whose sales are most recent. If only one
        # side has sales, that side is canonical (the other is dormant).
        # If neither has sales, skip — nothing useful to align.
        with_sales = [(tt, windows[tt]) for tt in tt_ids if tt in windows]
        if not with_sales:
            continue
        with_sales.sort(key=lambda p: p[1].last_sale, reverse=True)
        canonical_id = with_sales[0][0]
        for tt_id in tt_ids:
            if tt_id == canonical_id:
                continue
            if existing.get(tt_id) == canonical_id:
                continue
            a_w = windows.get(tt_id)
            c_w = windows.get(canonical_id)
            gap = ((c_w.first_sale - a_w.last_sale).days
                   if (a_w and c_w and a_w.last_sale and c_w.first_sale)
                   else None)
            key = (tt_id, canonical_id)
            if key in suggestions:
                # Upgrade an earlier suggestion's reason — catalog match is
                # the strongest signal we have.
                suggestions[key].reason = "catalog_same_sku_code+other"
                suggestions[key].confidence = "high"
            else:
                suggestions[key] = AliasSuggestion(
                    alias_sku=tt_id,
                    canonical_sku=canonical_id,
                    reason="catalog_same_sku_code",
                    confidence="medium",
                    alias_last_sale=a_w.last_sale if a_w else None,
                    canonical_first_sale=c_w.first_sale if c_w else None,
                    alias_total_units=a_w.total_units if a_w else 0,
                    canonical_total_units=c_w.total_units if c_w else 0,
                    days_gap=gap,
                )

    # ---- Heuristic 2: temporal handoff ---------------------------------
    today = datetime.now().date()
    dormant_cutoff = today - timedelta(days=quiet_window_days)
    dormant = [w for w in eligible.values() if w.last_sale <= dormant_cutoff]
    active = [w for w in eligible.values() if w.last_sale > dormant_cutoff]

    for a in dormant:
        for c in active:
            if a.sku == c.sku:
                continue
            gap = (c.first_sale - a.last_sale).days
            if not (-3 <= gap <= max_handoff_gap_days):
                # Handoff window: B can start a few days before A's last
                # sale (overlap) or up to max_handoff_gap_days after.
                continue
            if existing.get(a.sku) == c.sku:
                continue
            # Skip if we already have it from the stem heuristic — upgrade
            # the reason but don't duplicate.
            key = (a.sku, c.sku)
            if key in suggestions:
                suggestions[key].reason = "both"
                suggestions[key].confidence = "high"
                continue
            suggestions[key] = AliasSuggestion(
                alias_sku=a.sku,
                canonical_sku=c.sku,
                reason="temporal_handoff",
                confidence="medium",
                alias_last_sale=a.last_sale,
                canonical_first_sale=c.first_sale,
                alias_total_units=a.total_units,
                canonical_total_units=c.total_units,
                days_gap=gap,
            )

    # Stable sort: high confidence first, then by alias_sku.
    confidence_rank = {"high": 0, "medium": 1, "low": 2}
    return sorted(
        suggestions.values(),
        key=lambda s: (confidence_rank.get(s.confidence, 99), s.alias_sku),
    )


# ---- CLI -------------------------------------------------------------------

def _format_suggestions(suggestions: list[AliasSuggestion]) -> str:
    if not suggestions:
        return "No alias candidates found.\n"

    lines = []
    lines.append("")
    lines.append(f"  {'alias':18} -> {'canonical':18}  {'reason':18} {'conf':6}  "
                 f"{'alias_units':>11}  {'canon_units':>11}  {'gap':>6}")
    lines.append(f"  {'-'*18}    {'-'*18}  {'-'*18} {'-'*6}  "
                 f"{'-'*11}  {'-'*11}  {'-'*6}")
    for s in suggestions:
        gap = f"{s.days_gap}d" if s.days_gap is not None else "n/a"
        lines.append(
            f"  {s.alias_sku[:18]:18} -> {s.canonical_sku[:18]:18}  "
            f"{s.reason:18} {s.confidence:6}  "
            f"{s.alias_total_units:>11}  {s.canonical_total_units:>11}  {gap:>6}"
        )
    lines.append("")
    lines.append(f"  {len(suggestions)} candidate pair(s). Review and run "
                 "`apply <alias> <canonical>` for each one to keep.")
    return "\n".join(lines) + "\n"


def _format_suggestions_verbose(
    db: Session, suggestions: list[AliasSuggestion],
) -> str:
    """Same as `_format_suggestions` but also resolves each TikTok-ID SKU
    code to its catalog row (sku + name) so the buyer can identify what
    products are being suggested without a separate DB lookup. Useful when
    the codes are opaque numeric IDs (which they are after the order-import
    SKU resolver canonicalises everything to TikTok IDs)."""
    from app.models.sku import Sku
    from sqlalchemy import or_

    if not suggestions:
        return "No alias candidates found.\n"

    # Bulk-fetch all SKU codes referenced by the suggestions in one query.
    all_codes = set()
    for s in suggestions:
        all_codes.add(s.alias_sku)
        all_codes.add(s.canonical_sku)
    rows = db.execute(
        select(Sku).where(
            or_(
                Sku.tiktok_sku_id.in_(all_codes),
                Sku.sku.in_(all_codes),
                Sku.tiktok_alt_sku.in_(all_codes),
            )
        )
    ).scalars().all()
    by_code: dict[str, Sku] = {}
    for sku_row in rows:
        for key in (sku_row.tiktok_sku_id, sku_row.sku, sku_row.tiktok_alt_sku):
            if key and key in all_codes:
                by_code[str(key)] = sku_row

    def _label(code: str) -> str:
        s = by_code.get(code)
        if s is None:
            return "(not in catalog)"
        name = (s.name or "").strip()
        return f"{s.sku} — {name[:60]}"

    lines: list[str] = [""]
    for s in suggestions:
        gap = f"{s.days_gap}d" if s.days_gap is not None else "n/a"
        lines.append(f"  {s.reason:18} {s.confidence:6}  "
                     f"alias_units={s.alias_total_units:>5}  "
                     f"canon_units={s.canonical_total_units:>5}  gap={gap}")
        lines.append(f"    alias     {s.alias_sku}")
        lines.append(f"              {_label(s.alias_sku)}")
        lines.append(f"    canonical {s.canonical_sku}")
        lines.append(f"              {_label(s.canonical_sku)}")
        lines.append("")
    lines.append(f"  {len(suggestions)} candidate pair(s). Review and run "
                 "`apply <alias> <canonical>` for each one to keep.")
    return "\n".join(lines) + "\n"


def _format_list(aliases: list[SkuAlias]) -> str:
    if not aliases:
        return "No aliases registered.\n"
    lines = [
        "",
        f"  {'alias':22} -> {'canonical':22}  {'notes'}",
        f"  {'-'*22}    {'-'*22}  {'-'*40}",
    ]
    for a in aliases:
        lines.append(
            f"  {a.alias_sku[:22]:22} -> {a.canonical_sku[:22]:22}  {a.notes or ''}"
        )
    return "\n".join(lines) + "\n"


def cli_main(argv: list[str] | None = None) -> int:
    import argparse
    from app.db import Base, SessionLocal, engine

    # The web app's startup creates tables via Base.metadata.create_all, but
    # CLI invocations skip that — call it here so first-time use of this
    # tool doesn't crash on a missing sku_aliases table. Idempotent.
    import app.models  # noqa: F401 — register all models with Base.metadata
    Base.metadata.create_all(bind=engine)

    parser = argparse.ArgumentParser(prog="sku_alias")
    sub = parser.add_subparsers(dest="cmd", required=True)

    suggest_p = sub.add_parser("suggest", help="Print candidate alias pairs from current data.")
    suggest_p.add_argument("--verbose", "-v", action="store_true",
                           help="Resolve each TikTok-ID code to its catalog row "
                                "(sku + name) so the buyer can identify products.")
    sub.add_parser("list", help="Print all currently-registered aliases.")
    apply = sub.add_parser("apply", help="Register one alias mapping.")
    apply.add_argument("alias")
    apply.add_argument("canonical")
    apply.add_argument("--note", default=None)
    delete = sub.add_parser("delete", help="Remove one alias by alias_sku.")
    delete.add_argument("alias")

    args = parser.parse_args(argv)

    with SessionLocal() as db:
        if args.cmd == "suggest":
            suggestions = suggest_aliases(db)
            if args.verbose:
                print(_format_suggestions_verbose(db, suggestions), end="", flush=True)
            else:
                print(_format_suggestions(suggestions), end="", flush=True)
        elif args.cmd == "list":
            rows = db.execute(select(SkuAlias).order_by(SkuAlias.alias_sku)).scalars().all()
            print(_format_list(rows), end="", flush=True)
        elif args.cmd == "apply":
            upsert_alias(db, alias_sku=args.alias, canonical_sku=args.canonical, notes=args.note)
            db.commit()
            print(f"Aliased {args.alias} -> {args.canonical}", flush=True)
        elif args.cmd == "delete":
            n = db.execute(
                select(SkuAlias).where(SkuAlias.alias_sku == args.alias)
            ).scalar_one_or_none()
            if n is None:
                print(f"No alias found for {args.alias}", flush=True)
                return 1
            db.delete(n)
            db.commit()
            print(f"Deleted alias {args.alias}", flush=True)
        else:
            parser.print_help()
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
