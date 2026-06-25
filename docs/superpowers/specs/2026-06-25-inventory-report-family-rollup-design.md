# Inventory Report — shade/size family rollup + readability

**Date:** 2026-06-25
**Status:** Approved (brainstorming) — implementing
**Page:** `/reports/inventory` (`app/reports/inventory_report.py` + `app/templates/reports/inventory_report.html`)

## Problem

The inventory report lists one row per physical SKU. Products sold in shade/size
ranges (e.g. HALO Healthy Glow Tinted Moisturizer = 29 shades, Always On Adaptive
Foundation = 20 shades) each have their *own* SBX code and own on-hand, so they
render as dozens of separate rows. The list is hard to scan. The user wants
"like items" collapsed onto one summary line that expands to per-shade detail,
plus a clearer layout, a stock-status badge, and a days-of-cover column.

## Investigation findings (why this shape)

- On-hand from SAP is keyed by the **physical SBX code** (`InventorySnapshot.sku`
  == `Sku.sku`), not per TikTok variation. 148/153 snapshot keys match `Sku.sku`.
- True TikTok variations (one SBX code → multiple `tiktok_sku_id`) exist for only
  **3 of 172** codes and *share one on-hand pool* — nothing meaningful to expand.
- The real clutter is **shade/size families**: distinct SBX codes that share a base
  product name. ~15 families, the largest being 29- and 20-shade ranges. Each shade
  has its own on-hand → per-member totals are real and worth showing.
- There is **no family field** in the catalog. The family key is derived from the
  product name (decided: derive-from-name, not a new column, this pass).

## Design

### Grouping
- **Family key** = `Sku.name` with the trailing parenthesized size stripped
  (reuse `strip_size`) and any trailing ` - <shade>` segment removed (split on
  `" - "`, spaces required so `ALL-IN-ONE` / `ANTI-REDNESS` are not split),
  normalized (upper, collapsed whitespace).
- Rows grouped by family key. A family with **≥2 members** → expandable parent
  summary row (collapsed by default). A family with **1 member**, bundles, and
  unmapped rows → normal flat rows (no arrow).
- Parent summary aggregates members: sellable / sample / total / on-order / value
  summed; a "N shades" count chip; per-unit COGS shown as "—" (varies by shade);
  each member row shows its own numbers.

### Status badge + days-of-cover (new columns)
- Reuse `compute_demand_planning_view(db)` (folds to physical `Sku.sku`, already
  drives the nav badge) for per-member **status** and **days-of-supply** — so the
  inventory report agrees with the Demand Planning page. Wrapped in try/except so a
  planner failure degrades to no-badge rather than breaking the report.
- Badge mapping: `out_of_stock`→**Out**, `at_risk`/`reorder_now`→**Low**,
  `healthy`→**Healthy**, `overstocked`→**Overstock**, `no_velocity`/`discontinued`
  /no-entry→**No sales** (neutral).
- **Days of cover** shown in days, weeks in tooltip. "No sales" when no velocity.
- **Parent**: days-of-cover = Σ member sellable ÷ Σ member daily velocity;
  status = most-urgent member status (so a stockout can't hide inside a collapsed
  family).

### Layout / readability
- Default order: most-urgent first (Out → Low → Healthy → Overstock → No sales),
  then alphabetical. Existing column sorts still work.
- Roomier table, status shown as a colored chip, out-of-stock de-emphasized.
- Preserved as-is: KPIs, search, stock filter, All/Sellable/Sample toggle,
  pagination, CSV / print / email. Sort/filter/pagination operate on **groups**
  (a parent + its members move as one unit).

### Data model
- `InventoryReportRow` keeps its fields; **add** `status: str` and
  `days_of_cover: Decimal | None` (per member).
- **New** `InventoryGroup` (key, label, sku_code, is_family, is_bundle, members,
  aggregated quantities/values, member_count, status, days_of_cover).
- `InventoryReportView` keeps the flat `rows` list (CSV / xlsx / email read it)
  and **adds** `groups: list[InventoryGroup]` (the template reads it).

### Scope boundaries
- CSV export stays **flat per-member**, with new `status` + `days_of_cover` columns.
- Weekly **email/xlsx unchanged** this pass.
- No catalog/schema migration (family derived from name).

## Test plan (TDD)
- Multi-member family rolls up into one parent with summed aggregates + correct
  member_count; members preserved underneath.
- Single-member product / bundle / unmapped stays a flat group (is_family False).
- Family key derivation: `" - SHADE"` split, size-paren strip, `ALL-IN-ONE` not split.
- Status badge mapping + parent = most-urgent member.
- Days-of-cover: per member from planner; parent = blended; "no sales" path.
- Valuation invariant preserved (sample $0 COGS) — existing test still green.
