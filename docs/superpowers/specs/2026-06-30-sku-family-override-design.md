# SKU Family override — manual family grouping for the inventory report

**Date:** 2026-06-30
**Status:** Approved — implementing

## Problem
The inventory report auto-groups shade families by the **SBX code base** (strip the
trailing 2-digit shade). That works for `C5JK01…C5JK22`-style lines but misses lines
coded differently — e.g. *Halo Sculpt Palette* (`C73H01/J01/K01/L01` — shade is the
4th letter) and *Cali Contour Palette* (`C70N01`, `C49T01` — unrelated codes, same
product). No single code/name heuristic groups all of these reliably.

## Solution
An explicit **`family`** field on each SKU. When set, the inventory report groups by
it (overriding the auto code-rule); when blank, the auto-rule still applies. The
operator sets it self-service on the Manage SKUs grid (bulk-edit several at once).

## Design

### 1. Data model
- Add `family: str | None` (`String(128)`, nullable, indexed) to `Sku`.
- Alembic migration (down_revision = current head). `test_migrations` parity.

### 2. Inventory report grouping (`app/reports/inventory_report.py`)
- `InventoryReportRow` gains `family_override: str | None` (set from `Sku.family`).
- `_build_groups`: group key = `r.family_override` (when set) else `_family_key(r.sku_code)`
  for mapped non-bundle rows. A family-override group's display **label** is the
  `family` value the operator typed (else `_common_label` for code-base groups).

### 3. Catalog editing (`app/routers/admin.py` + `app/templates/admin/_skus_body.html`)
- `_sku_view` includes `family`.
- Grid: add a **Family** column (display).
- **Bulk edit** (the primary path): add a "Family" checkbox + text input to the bulk
  modal — the existing generic bulk JS auto-posts `apply_family` / `family`. The
  `bulk_edit_skus` route gains `apply_family: bool` + `family: str` and sets
  `sku.family = family.strip() or None` for the batch (blank clears, like the other
  bulk fields).
- `create_sku` (+ the Add SKU form): add an optional `family` field.

### 4. Seed the two reported families
After deploy, set `family` on the flagged SKUs (via the new mechanism):
- `Halo Sculpt + Glow Palette`: `SBX-C73H01/J01/K01/L01`
- `Cali Contour Palette`: `SBX-C70N01`, `SBX-C49T01`

## Tests (TDD)
- `compute_inventory_report`: SKUs sharing a `family` value group into one family
  (even with unrelated code bases); the family label is the `family` value; a SKU
  with no override still groups by the code-rule.
- `bulk_edit_skus`: `apply_family` sets `family` on the selected SKUs; blank clears.
- `create_sku`: persists `family`.
- Migration parity.

## Scope boundaries
Single-SKU drawer edit of `family` is out of scope (bulk-edit + Add SKU cover it).
The auto code-rule and the hide-unmapped/zero rules are unchanged. Bundles are
unaffected (family is a Sku field).
