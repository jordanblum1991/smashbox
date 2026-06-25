# Sales report — refund rate + anomaly digest (sub-project B)

Second of the A→B→C sequence. Two independent pieces sharing
`compute_sku_performance`.

## B1 — Per-SKU refund rate (dollar-weighted)

**Constraint:** refunds are stored at the **Order** level (`Order.refunds`,
positive magnitude, back-filled from settlement), not per line.

**Attribution:** split each order's refund $ across its lines by gross share:
`line_refund = order.refunds × (line.gross_sales / order.gross_sales)`
(guard `order.gross_sales > 0`). Exact for single-SKU orders.

Per SKU over the window:
- `refunded_amount` = Σ attributed line_refund
- `refund_rate` = `refunded_amount / gross` × 100, 1-dp. `None` when gross = 0;
  `0.0` when no refunds. Only meaningful once a period settles (refunds lag) —
  acceptable, recent periods read ~0%.

`_paid_lines` extends its select with `OrderLine.gross_sales`,
`Order.gross_sales`, `Order.refunds`. New `SkuPerfRow` fields:
`refunded_amount: Decimal`, `refund_rate: Decimal | None`.

**UI:** "Refund %" column on the SKU table — neutral, with subtle color when
elevated (amber ≥ 5%, red ≥ 10%). **CSV:** add `Refunded $`, `Refund %`.

## B2 — "Needs attention" digest in the scheduled sales email

In `send_sales_report`, compute the SKU view for the same window and add a
**Needs attention** card above the velocity table. Four categories (only
rendered when non-empty), each a short list of SKU code + the relevant metric,
capped at the top 5 with "+N more":

| Category | Rule |
|---|---|
| Decelerating | `status == "declining"` (momentum < −25% vs prior period) |
| Spiking | `momentum.pct is not None and momentum.pct > 50` |
| Stalled | `status == "stalled"` (sold before, 0 this period) |
| Low cover | `days_of_cover is not None and days_of_cover < 14` (red zone) |

Cap/sort: within each category, sort by units desc (decelerating/spiking/stalled)
or by cover asc (low cover); show ≤5, note the remainder. Pure helper
`build_attention_digest(view) -> dict[category, list[row]]` so it's unit-testable
independent of email rendering. HTML uses the existing `report_email_common`
styles; text body gets a plain-list mirror.

## Testing (TDD)

- **B1:** single-SKU order with a refund → exact rate; multi-SKU order →
  gross-share split; no refund → 0.0; gross 0 → None. CSV columns + a value.
  Refund % column renders (smoke).
- **B2:** `build_attention_digest` buckets SKUs into the four categories on a
  seeded fixture (one of each); empty categories omitted; cap-at-5 +N. Email
  render includes a "Needs attention" block when anomalies exist and omits it
  cleanly when none.

## Files

- `app/reports/sku_performance.py` — refund attribution, two fields, CSV cols.
- `app/services/sales_report_email.py` — `build_attention_digest` + render.
- `app/templates/reports/sales.html` — Refund % column (+ colspans → 10).
- `tests/` — extend sku_performance / CSV / skus-tab; new email-digest tests.

## Out of scope

On-page anomaly banner (email only for now); refund rate by units (we use the
dollar-weighted basis chosen in brainstorming).
