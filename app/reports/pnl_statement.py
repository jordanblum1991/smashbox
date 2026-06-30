"""P&L statement helpers shared by the per-fiscal-month CSV + PDF downloads.

Two pieces:
  - `statement_lines(pnl)` — the canonical P&L waterfall (label, signed amount,
    kind) so CSV and PDF render identical numbers. Mirrors the `monthly-pnl.xlsx`
    line list and uses the MANAGED figures (Smashbox-/TikTok-funded discount
    offsets applied) so every format agrees with the on-screen P&L.
  - `available_fiscal_months(db)` — the fiscal months that have data, newest
    first, for the downloads page.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.order import Order
from app.reports.fiscal_calendar import fiscal_range_str
from app.reports.monthly_pnl import MonthlyPnL
from app.reports.sales_report import current_fiscal_ym
from app.services.reporting_tz import today_local


@dataclass
class StatementLine:
    label: str
    amount: Decimal | None        # signed as it should display; None = label-only note
    kind: str                     # line | deduction | credit | subtotal | total | memo | subline | margin


@dataclass
class FiscalMonthRef:
    year: int
    month: int
    label: str                    # "Fiscal May 2026"
    range_str: str                # "Apr 29, 2026 – May 28, 2026"


def statement_lines(pnl: MonthlyPnL) -> list[StatementLine]:
    """The P&L waterfall as ordered display lines. Deductions are negative;
    credits/add-backs positive; the Net Profit and margins use the managed view."""
    L = StatementLine
    lines: list[StatementLine] = [
        L("Gross Product Sales", pnl.gross_sales, "line"),
        L("GMV (TikTok Seller Center)", pnl.gmv, "memo"),
        L("Less: TikTok-Funded Discount", -pnl.platform_discount, "deduction"),
        L("Less: Outlandish-Funded Discount", -pnl.outlandish_discount, "deduction"),
        L("Less: Smashbox-Funded Discount", -pnl.smashbox_discount, "deduction"),
        L("Smashbox-Funded Discount Reimbursed by Smashbox (contra entry)",
          pnl.smashbox_discount_offset, "memo"),
        L("Sales (after Discounts)", pnl.managed_sales_pre_refund, "subtotal"),
        L("Less: Refunds", -pnl.refunds, "deduction"),
        L("Net Customer Sales", pnl.managed_net_customer_sales, "subtotal"),
        L("COGS", -pnl.cogs, "deduction"),
        L("Gross Profit", pnl.managed_gross_profit, "subtotal"),
        L("TikTok fees", -pnl.tiktok_fees, "deduction"),
        L("Referral fee", -pnl.tiktok_referral_fee, "subline"),
        L("Transaction fee", -pnl.tiktok_transaction_fee, "subline"),
        L("Refund admin fee", -pnl.tiktok_refund_admin_fee, "subline"),
        L("Sales tax on referral", -pnl.tiktok_sales_tax_on_referral, "subline"),
        L("Smart promo fee (incl. tax)", -pnl.tiktok_smart_promo_fee, "subline"),
        L("Campaign fees (resource + service)", -pnl.tiktok_campaign_fees, "subline"),
        L("Shop partner commission", -pnl.tiktok_partner_commission, "subline"),
        L("Managed service (incl. tax)", -pnl.tiktok_managed_service, "subline"),
        L("Affiliate Commissions", -pnl.affiliate_commission, "deduction"),
        L("Shop Ads Commission", -pnl.shop_ads_cost, "deduction"),
        L("TikTok Ads (GMV Max)", -pnl.gmv_max_ad_spend, "deduction"),
        L("Less: GMV Max Reimbursement", pnl.gmv_max_reimbursement, "credit"),
        L("Less: Ad Credits", pnl.ad_credit_offset, "credit"),
        L("Shipping revenue", pnl.shipping_revenue, "line"),
        L("Shipping (to Customers)", -pnl.shipping_cost, "deduction"),
        L("Shipping (to Creators)", -pnl.sample_shipping_cost, "deduction"),
        L("TikTok Reimbursements & Adjustments", pnl.tiktok_adjustments_net, "line"),
        L("Net Profit", pnl.managed_net_profit, "total"),
        L("Gross Margin", pnl.managed_gross_margin, "margin"),
        L("Net Margin", pnl.managed_net_margin, "margin"),
    ]
    return lines


def _fiscal_months_between(start: tuple[int, int], end: tuple[int, int]) -> list[tuple[int, int]]:
    """Inclusive (year, month) list from start up to end, ascending."""
    out: list[tuple[int, int]] = []
    y, m = start
    while (y, m) <= end:
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def available_fiscal_months(
    db: Session, *, as_of: date | None = None,
) -> list[FiscalMonthRef]:
    """Fiscal months from the earliest order's fiscal month through the current
    fiscal month, NEWEST FIRST. Empty when there are no orders."""
    earliest = db.execute(select(func.min(Order.placed_at))).scalar()
    if earliest is None:
        return []
    today = as_of or today_local()
    start_ym = current_fiscal_ym(earliest.date())
    end_ym = current_fiscal_ym(today)
    if end_ym < start_ym:                       # all data in the future (shouldn't happen)
        end_ym = start_ym
    pairs = _fiscal_months_between(start_ym, end_ym)
    refs = [
        FiscalMonthRef(year=y, month=m,
                       # Plain month label on the page (e.g. "Feb 2026"); the
                       # 29th–28th window is shown in range_str alongside.
                       label=f"{calendar.month_abbr[m]} {y}",
                       range_str=fiscal_range_str(y, m, "month"))
        for y, m in pairs
    ]
    refs.reverse()                              # newest first
    return refs
