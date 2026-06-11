"""Golden baseline generator for the SQLite -> Postgres migration.

Reads a LOCAL prod snapshot (default: scratch/smashbox-prod-snapshot.db, which is
gitignored and must never be committed) READ-ONLY and emits:

  1. baseline/golden_schema.sql   -- full DDL dump
  2. baseline/baseline_report.md  -- row counts + curated financial-column totals

Pure stdlib. Uses Python Decimal for the money sums (NOT SQL SUM) so the totals
are exact and free of float drift -- this is a checksum we will diff against the
Postgres copy after migration, so exactness matters.

Run:  py baseline/generate_baseline.py [path-to-snapshot.db]
"""
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SNAPSHOT = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "scratch" / "smashbox-prod-snapshot.db"
SCHEMA_OUT = REPO / "baseline" / "golden_schema.sql"
REPORT_OUT = REPO / "baseline" / "baseline_report.md"

# ---------------------------------------------------------------------------
# CURATED financial-column selection. Every entry is a money column worth
# checksumming for the migration. Quantity/count ints (orders, items_sold,
# on_hand, quantity, sku_orders) and per-unit catalog reference costs
# (Sku.unit_cogs, Bundle component costs) are DELIBERATELY excluded from the
# financial totals -- they are not transactional money figures. They are still
# covered by the row-count section.
#
# NOTE on rollups: in `orders` and `settlements`, `tiktok_fees` is the sum of
# the 8 tiktok_* sub-buckets; totaling both is intentional (more checksums) but
# do NOT add them together when reasoning about net P&L.
# ---------------------------------------------------------------------------
FINANCIAL_COLUMNS = {
    "orders": [
        "gross_sales", "platform_discount_total", "refunds",
        "shipping_revenue", "shipping_cost",
        "tiktok_fees",  # rollup of the 8 below
        "tiktok_referral_fee", "tiktok_transaction_fee", "tiktok_refund_admin_fee",
        "tiktok_sales_tax_on_referral", "tiktok_smart_promo_fee",
        "tiktok_campaign_fees", "tiktok_partner_commission", "tiktok_managed_service",
        "affiliate_commission", "shop_ads_cost",
        "seller_funded_discount_total", "seller_funded_outlandish", "seller_funded_smashbox",
        "payment_platform_discount",
    ],
    "order_lines": [
        "gross_sales", "platform_discount", "post_tiktok_price",
        "seller_funded_discount", "seller_funded_outlandish", "seller_funded_smashbox",
    ],
    "settlements": [
        "order_income", "order_cost", "net_order_margin",
        "gross_sales", "gross_sales_refund", "seller_discount", "seller_discount_refund",
        "tiktok_fees",  # rollup of the 8 below
        "tiktok_referral_fee", "tiktok_transaction_fee", "tiktok_refund_admin_fee",
        "tiktok_sales_tax_on_referral", "tiktok_smart_promo_fee",
        "tiktok_campaign_fees", "tiktok_partner_commission", "tiktok_managed_service",
        "affiliate_commission", "shop_ads_cost", "shipping_cost",
    ],
    "adjustments": ["amount"],
    "payouts": ["gross_amount", "fees", "net_amount"],
    "ad_spend": ["cash_cost", "credit_cost", "ad_credit_cost", "amount"],
    "ad_credits": ["amount"],
    "gmv_max_reimbursements": ["amount"],
    "gmv_max_daily_metrics": ["cost", "gross_revenue"],
    # Orphan table — no ORM model in app/models/ (legacy GMV Max campaign data,
    # superseded by gmv_max_daily_metrics) — but it still carries real money, so
    # the migration checksum covers it. (sample_allowances is the other
    # model-less table; it is quantity-only, no money column, so it is excluded.)
    "gmv_max_campaign_metrics": ["gross_revenue"],
    "tiktok_daily_metrics": ["gmv", "gmv_with_tax", "tax", "shipping_fees"],
    "invoices": ["amount"],
    "purchase_invoices": ["amount"],
    "purchase_invoice_credits": ["amount"],
    "purchase_invoice_payments": ["amount"],
    "samples": ["shipping_cost"],
    "sample_inventory_movements": ["unit_cost"],
}


def as_decimal(v):
    """Exact Decimal from a stored SQLite value. str(float) yields the shortest
    round-trip repr, which recovers the intended 2-4dp value the app persisted."""
    if v is None:
        return None
    return Decimal(str(v))


def main():
    if not SNAPSHOT.exists():
        sys.exit(f"Snapshot not found: {SNAPSHOT}")

    con = sqlite3.connect(f"file:{SNAPSHOT}?mode=ro", uri=True)
    cur = con.cursor()

    # --- 1. schema dump --------------------------------------------------
    rows = cur.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL ORDER BY "
        "CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 "
        "WHEN 'trigger' THEN 2 WHEN 'view' THEN 3 ELSE 4 END, name"
    ).fetchall()
    with open(SCHEMA_OUT, "w", encoding="utf-8") as fh:
        fh.write("-- Golden schema dump from prod snapshot\n")
        fh.write(f"-- source: {SNAPSHOT.name}\n\n")
        for _type, name, sql in rows:
            fh.write(f"{sql.strip()};\n\n")

    # --- 2. row counts (every table) -------------------------------------
    tables = [r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()]
    counts = {t: cur.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}

    # --- 3. financial totals (curated columns) ---------------------------
    totals = {}        # table -> {col -> (total Decimal, nonnull, nulls)}
    missing = []       # (table, col) chosen but not present
    for table, cols in FINANCIAL_COLUMNS.items():
        if table not in counts:
            missing.append((table, "<TABLE MISSING>"))
            continue
        present = {r[1] for r in cur.execute(f'PRAGMA table_info("{table}")').fetchall()}
        totals[table] = {}
        for col in cols:
            if col not in present:
                missing.append((table, col))
                continue
            vals = [as_decimal(r[0]) for r in cur.execute(f'SELECT "{col}" FROM "{table}"').fetchall()]
            nonnull = [v for v in vals if v is not None]
            total = sum(nonnull, Decimal("0"))
            totals[table][col] = (total, len(nonnull), len(vals) - len(nonnull))

    con.close()

    # --- write report ----------------------------------------------------
    def money(d):
        return f"{d:,.2f}"

    lines = []
    lines.append("# Golden Baseline Report — prod SQLite snapshot")
    lines.append("")
    lines.append(f"- Source snapshot: `scratch/{SNAPSHOT.name}` (gitignored, not committed)")
    lines.append(f"- Tables: {len(tables)}")
    lines.append("- Money sums computed in Python `Decimal` (no float/SQL-SUM drift).")
    lines.append("- Two prod tables have no ORM model: `gmv_max_campaign_metrics` "
                 "(money column `gross_revenue` IS totaled below) and "
                 "`sample_allowances` (quantity-only, no money — excluded).")
    lines.append("- **STATUS: Coverage finalized 2026-06-11.**")
    lines.append("")

    lines.append("## Row counts (all tables)")
    lines.append("")
    lines.append("| Table | Rows |")
    lines.append("|---|---:|")
    for t in sorted(counts):
        lines.append(f"| {t} | {counts[t]:,} |")
    lines.append(f"| **TOTAL ROWS** | **{sum(counts.values()):,}** |")
    lines.append("")

    lines.append("## Financial column totals")
    lines.append("")
    lines.append("`nn` = non-null rows summed, `nulls` = null rows skipped.")
    lines.append("")
    lines.append("| Table | Column | Total | nn | nulls |")
    lines.append("|---|---|---:|---:|---:|")
    for table in FINANCIAL_COLUMNS:
        if table not in totals:
            continue
        for col, (total, nn, nulls) in totals[table].items():
            lines.append(f"| {table} | {col} | {money(total)} | {nn:,} | {nulls:,} |")
    lines.append("")

    # invariant spot-check: seller-funded split must sum exactly
    lines.append("## Invariant spot-checks")
    lines.append("")
    for table in ("orders", "order_lines"):
        td = totals.get(table, {})
        out = td.get("seller_funded_outlandish")
        sm = td.get("seller_funded_smashbox")
        tot_key = "seller_funded_discount_total" if table == "orders" else "seller_funded_discount"
        tot = td.get(tot_key)
        if out and sm and tot:
            lhs = out[0] + sm[0]
            ok = "OK" if lhs == tot[0] else "MISMATCH"
            lines.append(
                f"- `{table}`: outlandish + smashbox = {money(lhs)} vs total {money(tot[0])} -> **{ok}**"
            )
    lines.append("")

    if missing:
        lines.append("## Chosen columns NOT found (coverage gaps)")
        lines.append("")
        for t, c in missing:
            lines.append(f"- {t}.{c}")
        lines.append("")
    else:
        lines.append("_All curated financial columns were present in the snapshot._")
        lines.append("")

    REPORT_OUT.write_text("\n".join(lines), encoding="utf-8")

    # console summary
    print(f"Schema  -> {SCHEMA_OUT}")
    print(f"Report  -> {REPORT_OUT}")
    print(f"Tables: {len(tables)}  Total rows: {sum(counts.values()):,}")
    if missing:
        print(f"COVERAGE GAPS: {missing}")
    else:
        print("Coverage: all curated financial columns present.")


if __name__ == "__main__":
    main()
