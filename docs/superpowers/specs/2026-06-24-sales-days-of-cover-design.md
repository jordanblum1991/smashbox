# Sales report — days-of-cover bridge (sub-project A)

## Goal

Make the per-SKU sales view actionable for buying by showing **days of cover**
inline: how long current on-hand lasts at the SKU's selling rate. First of the
A→B→C sequence (cover → refunds/anomaly → drill-down); the signal is reused by
the drill-down (C).

## Decisions (from brainstorming)

- **Velocity basis:** the selected-period **avg units/day (calendar)** already
  computed in `SkuStats.avg_units_per_day` — self-consistent with the on-screen
  numbers.
- **Placement:** a compact inline **Cover** column on the SKU table, color-coded
  when low.

## Computation

- **On-hand** = latest **sellable** snapshot (`InventorySnapshot`, SB warehouse)
  for the SKU's physical code, reusing the demand-planner helpers
  (`_latest_on_hand_per_sku` → `_physical_key_resolver` → `_fold_on_hand_to_physical`)
  so it's keyed by `Sku.sku`. The sales row's `code` IS `Sku.sku` (from
  `catalog_label_map`), so lookup is `on_hand_by_physical.get(row.code)`.
  Computed once per report call (one map), not per row.
  - Bundles (`code = bundle_sku`, no snapshot), unmapped SKUs, and SKUs with no
    snapshot → `on_hand = None`.
- **days_of_cover** = `on_hand / avg_units_per_day`, quantized to 0.1.
  - `None` when `on_hand is None` or `avg_units_per_day == 0` (active rows always
    have avg/day > 0, so the latter is just a guard).
  - `on_hand == 0` → `0.0` (out of stock — a real, useful signal).

New fields on `SkuPerfRow`: `on_hand: int | None`, `days_of_cover: Decimal | None`.

## UI

New **Cover** column on the SKU table:
- `None` → "—" (no inventory data / bundle / unmapped).
- else "{days}d", color-coded by stockout risk (aligned with the demand
  planner's ~35-day target = 14d lead + 21d cover):
  - red (`text-rose-600`) when `< 14` (won't survive a reorder cycle, incl. 0d),
  - amber (`text-amber-600`) when `14 ≤ cover < 35`,
  - green (`text-emerald-600`) when `≥ 35`.
- Classes are explicit (not interpolated) → no Tailwind safelist needed.

Column goes between Orders and % (or right after Net Sales) — placed where it
reads naturally; table grows from 8 to 9 columns; `colspan` for the empty-state
row and the expand detail row updated to 9.

## CSV

Append `On Hand` and `Days of Cover` to `SKU_CSV_HEADER` / `sku_performance_csv_rows`
(blank cells when `None`).

## Testing (TDD)

- `compute_sku_performance`: seed a SKU with a known on-hand snapshot + sales →
  assert `on_hand` and `days_of_cover` (on-hand ÷ avg/day). Edge: no snapshot →
  both `None`; on_hand 0 → cover 0.0; bundle row → `None`.
- CSV includes the two new columns + a known value.
- Table renders the Cover column (smoke).

## Out of scope

Bundle cover (component-min) — bundles show "—" for now. Per-component cover
belongs to the drill-down (C) if wanted.

## Files

- `app/reports/sku_performance.py` — on-hand map, two new fields, CSV cols.
- `app/templates/reports/sales.html` — Cover column + colspans.
- `tests/` — extend `test_sku_performance.py`, `test_sales_sku_csv.py`,
  `test_sales_skus_tab.py`.
