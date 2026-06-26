# Vendor AR invoice — void (soft delete)

## Goal

Let an admin cancel a Vendor AR invoice (`/admin/invoices`) that was **issued
but not paid**. Soft delete (void): the record + number are kept for audit,
marked `voided`, shown struck/greyed, and the invoice can no longer be paid.

Scope: Vendor AR `Invoice` only (the model already anticipates a `"voided"`
status). The Product AP side is untouched.

## Why void is contained here

`Invoice` (Vendor AR) has **no** dashboard/overdue coupling — `is_overdue`,
the aging report, and the due-soon banner are all on `PurchaseInvoice` (Product
AP). The vendor list shows only a count, no money totals. So voiding affects
only: the status badge, the status filter, and the "Mark Paid" availability.

## Status model

`status`: `issued` → `voided` (one-way). `paid` cannot be voided. `voided`
cannot be paid. Stored as the existing free-form string — no migration.

## Route

`POST /admin/invoices/{invoice_id}/void` (admin-only), mirroring `mark-paid`:
- not found → redirect `/admin/invoices` with `error`.
- `status == "paid"` → redirect to detail with `error` "Cannot void a paid invoice."
- `status == "issued"` → set `voided`, commit, redirect to detail with
  `notice` "Invoice {number} voided."
- `status == "voided"` → idempotent: redirect to detail with `notice`
  "Invoice already voided." (no commit).

## Guard: mark-paid

`invoice_mark_paid` must reject a voided invoice: `status == "voided"` →
redirect with `error` "Cannot mark a voided invoice as paid." (Today it only
short-circuits on `paid`.)

## UI

**Detail page** (`invoices_detail.html`):
- Status card: a third badge — **Voided** (slate, e.g. `bg-slate-100
  text-slate-600`).
- Header actions: a **Void invoice** button shown only when `status ==
  "issued"`, posting to the void route, gated by a JS `confirm()`. Hide Edit +
  Mark Paid when `voided`.

**Hub list** (`_invoices_vendor_body.html`):
- Status badge gains the **Voided** variant; voided rows render greyed
  (`text-slate-400`, number struck-through).
- Row actions: hide Edit + Mark Paid for voided (View / PDF stay).
- Mark-Paid visibility changes from `status != "paid"` to `status == "issued"`
  (so it never shows for paid OR voided).
- Status filter dropdown gains a **Voided** option (the existing
  `_filter_invoices` already filters by stored status, so no logic change).

CSV already emits `inv.status`, so `voided` flows through unchanged.

## Testing (TDD)

- Route: void an issued invoice → status `voided`, redirect + notice; voiding a
  **paid** invoice → unchanged + error; voiding again → idempotent; unknown id →
  redirect with error.
- mark-paid on a voided invoice → rejected, status stays `voided`.
- Hub renders the Voided badge + filter option; a voided row hides Edit/Mark-Paid.
- Detail page shows the Void button only for issued, Voided badge when voided.

## Out of scope

- "VOID" watermark on the PDF/preview (note for a follow-up; the document still
  renders normally).
- Editing a voided invoice (Edit hidden in UI; no hard route guard added now).
- Un-void / restore.

## Files

- `app/routers/invoices.py` — void route + mark-paid guard.
- `app/templates/admin/invoices_detail.html` — Void button + Voided badge.
- `app/templates/admin/_invoices_vendor_body.html` — badge, greyed row,
  action gating, filter option.
- `tests/test_invoices.py` (+ `test_invoices_hub.py`) — route + UI coverage.
