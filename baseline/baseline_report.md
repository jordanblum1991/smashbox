# Golden Baseline Report — prod SQLite snapshot

- Source snapshot: `scratch/smashbox-prod-snapshot.db` (gitignored, not committed)
- Tables: 27
- Money sums computed in Python `Decimal` (no float/SQL-SUM drift).
- **STATUS: DRAFT pending column-coverage confirmation.**

## Row counts (all tables)

| Table | Rows |
|---|---:|
| ad_credits | 4 |
| ad_spend | 242 |
| adjustments | 33 |
| bundle_components | 44 |
| bundles | 20 |
| creators | 0 |
| gmv_max_campaign_metrics | 4 |
| gmv_max_daily_metrics | 159 |
| gmv_max_reimbursements | 0 |
| import_batches | 78 |
| inventory_snapshots | 340 |
| invoices | 6 |
| order_lines | 2,989 |
| orders | 2,783 |
| payouts | 13 |
| purchase_invoice_credits | 6 |
| purchase_invoice_payments | 2 |
| purchase_invoices | 6 |
| sample_allowances | 2 |
| sample_inventory_movements | 0 |
| samples | 0 |
| settlements | 2,581 |
| shops | 1 |
| sku_aliases | 53 |
| skus | 176 |
| tiktok_daily_metrics | 129 |
| users | 5 |
| **TOTAL ROWS** | **9,676** |

## Financial column totals

`nn` = non-null rows summed, `nulls` = null rows skipped.

| Table | Column | Total | nn | nulls |
|---|---|---:|---:|---:|
| orders | gross_sales | 57,173.00 | 2,783 | 0 |
| orders | platform_discount_total | 9,491.37 | 2,783 | 0 |
| orders | refunds | 2,867.59 | 2,783 | 0 |
| orders | shipping_revenue | 121.04 | 2,783 | 0 |
| orders | shipping_cost | 10,945.26 | 2,783 | 0 |
| orders | tiktok_fees | 3,742.82 | 2,783 | 0 |
| orders | tiktok_referral_fee | 2,798.64 | 2,783 | 0 |
| orders | tiktok_transaction_fee | 0.00 | 2,783 | 0 |
| orders | tiktok_refund_admin_fee | 15.56 | 2,783 | 0 |
| orders | tiktok_sales_tax_on_referral | 0.00 | 2,783 | 0 |
| orders | tiktok_smart_promo_fee | 845.24 | 2,783 | 0 |
| orders | tiktok_campaign_fees | 83.38 | 2,783 | 0 |
| orders | tiktok_partner_commission | 0.00 | 2,783 | 0 |
| orders | tiktok_managed_service | 0.00 | 2,783 | 0 |
| orders | affiliate_commission | 1,942.82 | 2,783 | 0 |
| orders | shop_ads_cost | 802.36 | 2,783 | 0 |
| orders | seller_funded_discount_total | 5,565.39 | 2,783 | 0 |
| orders | seller_funded_outlandish | 2,501.10 | 2,783 | 0 |
| orders | seller_funded_smashbox | 3,064.29 | 2,783 | 0 |
| orders | payment_platform_discount | 40.22 | 2,783 | 0 |
| order_lines | gross_sales | 57,173.00 | 2,989 | 0 |
| order_lines | platform_discount | 9,491.37 | 2,989 | 0 |
| order_lines | post_tiktok_price | 47,681.63 | 2,989 | 0 |
| order_lines | seller_funded_discount | 5,565.39 | 2,989 | 0 |
| order_lines | seller_funded_outlandish | 2,501.10 | 2,989 | 0 |
| order_lines | seller_funded_smashbox | 3,064.29 | 2,989 | 0 |
| settlements | order_income | 44,337.09 | 2,581 | 0 |
| settlements | order_cost | -16,969.16 | 2,581 | 0 |
| settlements | net_order_margin | 27,367.93 | 2,581 | 0 |
| settlements | gross_sales | 51,657.00 | 2,581 | 0 |
| settlements | gross_sales_refund | 2,741.00 | 2,581 | 0 |
| settlements | seller_discount | 4,870.19 | 2,581 | 0 |
| settlements | seller_discount_refund | 291.28 | 2,581 | 0 |
| settlements | tiktok_fees | 3,742.82 | 2,581 | 0 |
| settlements | tiktok_referral_fee | 2,798.64 | 2,581 | 0 |
| settlements | tiktok_transaction_fee | 0.00 | 2,581 | 0 |
| settlements | tiktok_refund_admin_fee | 15.56 | 2,581 | 0 |
| settlements | tiktok_sales_tax_on_referral | 0.00 | 2,581 | 0 |
| settlements | tiktok_smart_promo_fee | 845.24 | 2,581 | 0 |
| settlements | tiktok_campaign_fees | 83.38 | 2,581 | 0 |
| settlements | tiktok_partner_commission | 0.00 | 2,581 | 0 |
| settlements | tiktok_managed_service | 0.00 | 2,581 | 0 |
| settlements | affiliate_commission | 1,942.82 | 2,581 | 0 |
| settlements | shop_ads_cost | 802.36 | 2,581 | 0 |
| settlements | shipping_cost | 10,945.26 | 2,581 | 0 |
| adjustments | amount | 1,888.10 | 33 | 0 |
| payouts | gross_amount | 42,177.19 | 13 | 0 |
| payouts | fees | 13,040.00 | 13 | 0 |
| payouts | net_amount | 29,137.19 | 13 | 0 |
| ad_spend | cash_cost | 0.00 | 242 | 0 |
| ad_spend | credit_cost | 19,285.72 | 242 | 0 |
| ad_spend | ad_credit_cost | 15,865.34 | 242 | 0 |
| ad_spend | amount | 35,151.06 | 242 | 0 |
| ad_credits | amount | 27,730.62 | 4 | 0 |
| gmv_max_reimbursements | amount | 0.00 | 0 | 0 |
| gmv_max_daily_metrics | cost | 34,970.57 | 159 | 0 |
| gmv_max_daily_metrics | gross_revenue | 47,751.57 | 159 | 0 |
| tiktok_daily_metrics | gmv | 41,070.86 | 129 | 0 |
| tiktok_daily_metrics | gmv_with_tax | 44,146.24 | 129 | 0 |
| tiktok_daily_metrics | tax | 3,227.36 | 129 | 0 |
| tiktok_daily_metrics | shipping_fees | 6,225.36 | 129 | 0 |
| invoices | amount | 29,865.56 | 6 | 0 |
| purchase_invoices | amount | 76,064.00 | 6 | 0 |
| purchase_invoice_credits | amount | 20,375.69 | 6 | 0 |
| purchase_invoice_payments | amount | 43,769.00 | 2 | 0 |
| samples | shipping_cost | 0.00 | 0 | 0 |
| sample_inventory_movements | unit_cost | 0.00 | 0 | 0 |

## Invariant spot-checks

- `orders`: outlandish + smashbox = 5,565.39 vs total 5,565.39 -> **OK**
- `order_lines`: outlandish + smashbox = 5,565.39 vs total 5,565.39 -> **OK**

_All curated financial columns were present in the snapshot._
