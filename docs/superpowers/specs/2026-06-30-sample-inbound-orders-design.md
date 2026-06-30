# Inbound sample inventory — sample inbound orders

**Date:** 2026-06-30
**Status:** Approved — implementing

## Goal
Track incoming (inbound) sample stock as **on-order sample inventory**, at **$0 cost**,
surfaced on the Sample Inventory report. SAP's SBS warehouse eventually counts the
stock once it arrives, so inbound is an **"on order" pipeline that clears on receipt**
— it must never touch (and never double-count) the SAP-fed on-hand.

## Context (existing)
- Sample on-hand = latest **SAP SBS snapshot** (`SampleInventorySnapshot`), read by
  `compute_sample_inventory_view` (`app/reports/sample_inventory.py`). Authoritative;
  do not change.
- Sellable inbound pattern to mirror: `PurchaseOrder` (draft→placed→received) +
  `compute_in_transit` (`app/reports/in_transit.py`) → on-order units keyed under all
  catalog identifiers; shown as the inventory report's "On order" column.
- `SampleInventoryMovement` ledger stays untouched (separate audit concern).

## Design

### 1. Data model (new, isolated tables)
- **`SampleInboundOrder`** (`sample_inbound_orders`): `id`, `shop_id` (nullable FK),
  `source` (free text), `status` (`open` | `received`, default `open`), `note`,
  `created_at`, `received_at` (nullable). Helper props mirror PO: `is_open`,
  `is_received`, `unit_count`, `status_label`.
- **`SampleInboundOrderLine`** (`sample_inbound_order_lines`): `id`,
  `sample_inbound_order_id` FK, `sku`, `name` (product-name snapshot), `quantity`.
  **No cost field** — sample stock is $0.
- Register both in `app/models/__init__.py`.
- **Alembic migration** creating the two tables (down_revision = current head).
  `tests/test_migrations.py` parity must stay green.

### 2. Inbound computation — `app/reports/sample_inbound.py`
- `compute_sample_inbound(db) -> {sku_key: units}` — mirrors `compute_in_transit`:
  sum lines of **`open`** orders, each qty replicated under every catalog identifier
  (tiktok_sku_id / sku / tiktok_alt_sku) so any lookup key matches. `received` excluded.
- `sample_inbound_summary(db) -> {open_orders, units_inbound}` — direct line sum (no
  replication) for headers.

### 3. Sample Inventory report enrichment (`app/reports/sample_inventory.py`)
- `SampleOnHandRow` gains `inbound_units: int` and `total_units` (= on_hand + inbound).
- The view iterates the **union** of on-hand keys and inbound keys, so inbound-only
  SKUs (incoming, no SAP on-hand yet) appear (on_hand 0). Inbound carries **$0 value**.
- `SampleInventoryView` gains `total_inbound_units` and `total_units`.
- Template `reports/sample_inventory.html`: add "Inbound (on order)" + "Total" columns
  and footer totals; a header link "Manage inbound sample orders".

### 4. Management page + routes — `app/routers/sample_inbound.py`
Lightweight (no PDF, no draft state):
- `GET /admin/sample-inbound` — list open + received orders (newest first) + a create form.
- `POST /admin/sample-inbound` — create an order (source/note + lines: sku, qty; name
  snapshot resolved from catalog where possible).
- `POST /admin/sample-inbound/{id}/receive` — set `status='received'`, `received_at=now`
  (drops it from `compute_sample_inbound`). **No ledger/snapshot write** (SAP owns on-hand).
- `POST /admin/sample-inbound/{id}/delete` — remove an order (open or received).
- Admin-gated (`require_admin`), mirroring the purchase-orders router.
- Template `admin/sample_inbound.html`.
- Nav: add under the Inventory dropdown ("Sample Inbound").

### 5. Receive behavior
`open` → counts as inbound. `received` → excluded from inbound (SAP on-hand now reflects
it). Received orders kept for history. Manual only; no auto-clear.

## Tests (TDD)
- `compute_sample_inbound`: open orders summed + replicated under all SKU identifiers;
  received excluded; empty → {}.
- `compute_sample_inventory_view`: inbound column + `total_units` = on_hand + inbound;
  an inbound-only SKU appears with on_hand 0; totals correct.
- Routes: create order, list renders, receive clears it from inbound, delete removes it.
- Migration parity (`tests/test_migrations.py`).

## Scope boundaries
$0 cost (no cost field/tracking); statuses open→received only (no draft/PDF); no SAP
changes; manual receive (no auto-clear); `SampleInventoryMovement` untouched; main
`/reports/inventory` unchanged (sample inbound shows on the Sample Inventory report only).
