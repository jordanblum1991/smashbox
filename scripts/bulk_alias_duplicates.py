"""One-shot: bulk-alias all duplicate sku_codes on the planner.

For every sku_code with multiple planner rows, picks a canonical (the
TikTok-ID-form key — the most stable identifier the order resolver
emits) and aliases all other forms to it. After this runs, every
physical product should be one row on the planner.

Modes:
  default      dry-run; prints what WOULD happen, makes no DB writes
  --apply      actually creates the SkuAlias rows + commits

Idempotent — pairs already in sku_aliases are skipped silently, so
reruns add only what's new.
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.reports.demand_planning import compute_demand_planning_view  # noqa: E402
from app.services.sku_alias import load_alias_map, upsert_alias  # noqa: E402


def _is_tiktok_id(key: str) -> bool:
    """A TikTok SKU ID looks like a 16-19 digit numeric string."""
    return key.isdigit() and len(key) >= 16


def _pick_canonical(rows) -> str:
    """For a group of duplicate rows, pick the canonical component_sku.

    Preference order:
      1. TikTok-ID form with the highest daily_velocity (the row that's
         actually receiving direct sales attribution).
      2. Any TikTok-ID form.
      3. Whichever row has the most velocity.
      4. The longest key (last resort).
    """
    tiktok_id_rows = [r for r in rows if _is_tiktok_id(r.component_sku)]
    if tiktok_id_rows:
        tiktok_id_rows.sort(key=lambda r: r.daily_velocity, reverse=True)
        return tiktok_id_rows[0].component_sku

    by_velocity = sorted(rows, key=lambda r: r.daily_velocity, reverse=True)
    if by_velocity[0].daily_velocity > 0:
        return by_velocity[0].component_sku

    return max(rows, key=lambda r: len(r.component_sku)).component_sku


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="Actually create the alias rows. "
                             "Without this flag, runs as a dry-run.")
    args = parser.parse_args()

    with SessionLocal() as db:
        view = compute_demand_planning_view(db)
        existing = load_alias_map(db)

        # Group rows by sku_code.
        by_code: dict[str, list] = defaultdict(list)
        for r in view.rows:
            if r.sku_code:
                by_code[r.sku_code].append(r)

        duplicates = {code: rows for code, rows in by_code.items() if len(rows) > 1}

        if not duplicates:
            print("No duplicate sku_codes. Nothing to do.")
            return 0

        # Compute the planned alias mappings.
        planned: list[tuple[str, str, str]] = []  # (alias, canonical, sku_code)
        skipped_already = 0
        for sku_code, rows in duplicates.items():
            canonical = _pick_canonical(rows)
            for r in rows:
                if r.component_sku == canonical:
                    continue
                if existing.get(r.component_sku) == canonical:
                    skipped_already += 1
                    continue
                planned.append((r.component_sku, canonical, sku_code))

        print(f"Duplicate sku_codes: {len(duplicates)}")
        print(f"Planned new aliases: {len(planned)}")
        if skipped_already:
            print(f"Already-aliased pairs skipped: {skipped_already}")
        print()
        print(f"  {'alias':24} -> {'canonical':24}  {'sku_code'}")
        print(f"  {'-'*24}    {'-'*24}  {'-'*20}")
        for alias_sku, canonical_sku, code in planned:
            print(f"  {alias_sku[:24]:24} -> {canonical_sku[:24]:24}  {code}")

        if not args.apply:
            print()
            print("Dry-run. Pass --apply to actually create these aliases.")
            return 0

        # Apply.
        print()
        print("Applying...")
        for alias_sku, canonical_sku, code in planned:
            upsert_alias(
                db,
                alias_sku=alias_sku,
                canonical_sku=canonical_sku,
                notes=f"Bulk dedup: {code} (alias = sales/bundle key, canonical = TikTok ID)",
            )
        db.commit()
        print(f"Done. {len(planned)} aliases created.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
