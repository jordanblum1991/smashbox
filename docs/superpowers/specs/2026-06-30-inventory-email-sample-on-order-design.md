# Inventory email — add sample on-order

**Date:** 2026-06-30
**Status:** Approved — implementing

## Goal
The weekly inventory snapshot email should show **both** on-order figures:
- **Sellable on-order** — already present as the "On Order" column (`in_transit`,
  placed Purchase Orders). Relabel for clarity.
- **Sample on-order** — NEW. Open sample inbound orders (`compute_sample_inbound`).

Email only (HTML + text + xlsx). The `/reports/inventory` web page and the Sample
Inventory report are unchanged.

## Design

### 1. Report view (`app/reports/inventory_report.py`)
- `InventoryReportRow` gains `sample_in_transit: int = 0`.
- `InventoryReportView` gains `total_sample_in_transit: int = 0`.
- `compute_inventory_report` folds `compute_sample_inbound(db)` (open sample inbound
  orders, keyed under every catalog identifier) onto each row by its keys —
  mirroring how `in_transit` is looked up. Total = `sample_inbound_summary` units
  (direct, non-replicated sum). The web template ignores the new field.

### 2. Email (`app/services/inventory_report_email.py`)
- **HTML** (`render_inventory_email`): relabel "On Order" → **"On order (sellable)"**;
  add **"On order (sample)"** column = `r.sample_in_transit`; add its column total.
- **Text**: same two columns + totals.
- **xlsx** (`build_inventory_xlsx`): same two columns + totals; widen/format to match.

Per-row order: SKU · Product · Sellable · Sample · Total · On order (sellable) ·
On order (sample).

## Tests (TDD)
- `compute_inventory_report`: a seeded open sample inbound order surfaces as
  `sample_in_transit` on the matching row + `total_sample_in_transit`; received
  orders don't count.
- Email: HTML + xlsx include "On order (sample)" / "On order (sellable)" headers and
  the per-row + total sample-on-order values.

## Scope boundaries
Email only; no migration (computed field); sample on-order = OPEN inbound orders;
no change to the web inventory report or Sample Inventory report.
