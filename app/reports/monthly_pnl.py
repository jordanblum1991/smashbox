"""Monthly P&L.

Aggregates PAID orders in the [month_start, next_month) window. Free samples
are excluded from revenue; their COGS lands in the sample-tracking report.

The discount section is presented as a waterfall — every line a separate
deduction so a reader can see exactly who funded what:

  Gross Product Sales
  − TikTok-Funded Discount    (TikTok promo; not our cost)
  − Outlandish-Funded Discount (first 10% of post-TikTok price)
  − Smashbox-Funded Discount   (residual seller-funded)
  − Refunds
  = Net Customer Sales         (a.k.a. Net Product Revenue)
"""
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.models.ad_credit import AdCredit
from app.models.ad_spend import AdSpend
from app.models.gmv_max_reimbursement import GmvMaxReimbursement
from app.models.order import Order, OrderLine, OrderType
from app.models.sample import Sample
from app.models.settlement import Adjustment
from app.models.sku import Sku


@dataclass
class MonthlyPnL:
    month: date

    # Revenue waterfall
    gross_sales: Decimal
    platform_discount: Decimal
    outlandish_discount: Decimal
    smashbox_discount: Decimal
    # TikTok-funded "Payment platform discount" — separate from
    # platform_discount (which is `SKU Platform Discount`). Subtracted in
    # TikTok's GMV formula alongside the SKU platform discount under
    # "Platform co-funding." Exposed via the `gmv` property below.
    payment_platform_discount: Decimal
    refunds: Decimal
    net_customer_sales: Decimal

    # Cost lines
    cogs: Decimal
    gross_profit: Decimal
    tiktok_fees: Decimal                       # rolled-up sum of the 8 below
    tiktok_referral_fee: Decimal
    tiktok_transaction_fee: Decimal
    tiktok_refund_admin_fee: Decimal
    tiktok_sales_tax_on_referral: Decimal
    tiktok_smart_promo_fee: Decimal
    tiktok_campaign_fees: Decimal
    tiktok_partner_commission: Decimal
    tiktok_managed_service: Decimal
    affiliate_commission: Decimal
    shop_ads_cost: Decimal
    gmv_max_ad_spend: Decimal                  # TikTok Ads (GMV Max) — from Cost export
    gmv_max_reimbursement: Decimal             # Manually-entered Smashbox-paid
                                               # reimbursement to Outlandish for GMV
                                               # Max spend. Independent pipeline from
                                               # ad_credit_offset; both can coexist.
    ad_credit_offset: Decimal                  # Manually-entered ad credits for the month
    shipping_revenue: Decimal
    shipping_cost: Decimal                     # PAID orders only (existing behavior)
    sample_shipping_cost: Decimal              # SAMPLE/PAID_SAMPLE order shipping +
                                               # off-platform Sample.shipping_cost
                                               # Captured separately so the operational
                                               # Shipping cost line stays paid-only.
    # Net sum of settlement-level adjustments dated in the window — TikTok
    # reimbursements (logistics/lost-package credits, Shop reimbursements,
    # bill payments) net of any deductions. Paired balance/deduction rows
    # cancel by design. Flows into Net Profit as Other Income.
    tiktok_adjustments_net: Decimal
    net_profit: Decimal

    # Per-type breakdown of the adjustments rollup above. Keys are
    # `Adjustment.adjustment_type` strings (TikTok categories like
    # "Logistics reimbursement", "TikTok Shop reimbursement", "Bill
    # payment (negative balance)", paired "Net earnings balance" /
    # "Net earnings deduction", etc.). Values are signed amounts —
    # positive for credits to us, negative for deductions from us.
    # Sum of all values equals `tiktok_adjustments_net`. The dashboard
    # uses this for the expandable detail under the adjustments line.
    tiktok_adjustments_by_type: dict[str, Decimal] = field(default_factory=dict)

    # Settlement coverage — what fraction of paid orders in this month have
    # been settled by TikTok (and therefore have fees / shipping / etc).
    # Pending orders still contribute gross sales and discount lines but
    # contribute $0 to the cost lines, so coverage < 100% means costs are
    # understated and net profit is overstated.
    orders_count: int = 0
    orders_settled: int = 0

    # Volume metrics for the operating-metrics tiles on the Dashboard.
    # `units_sold` is SUM(OrderLine.quantity) for PAID orders only — bundles
    # naturally count as 1 line (one OrderLine per bundle, qty unit). We do
    # NOT explode bundles into components here.
    units_sold: int = 0

    @property
    def settlement_coverage_pct(self) -> Decimal:
        if self.orders_count == 0:
            return Decimal("0")
        return (Decimal(self.orders_settled) / Decimal(self.orders_count)) * 100

    @property
    def aov_after_discounts(self) -> Decimal:
        """Average order value AFTER both TikTok-funded and seller-funded
        discounts (i.e. Net Customer Sales / orders_count). Uses the
        settlement-real net_customer_sales; use `managed_aov_after_discounts`
        for the operator/managed-P&L view (which adds the Smashbox offset back)."""
        if self.orders_count == 0:
            return Decimal("0")
        return self.net_customer_sales / Decimal(self.orders_count)

    # -----------------------------------------------------------------
    # MANAGED-P&L PROPERTIES — contra-net-zero presentation of the
    # Smashbox-Funded Discount.
    #
    # Smashbox funds the Smashbox-Funded portion of the seller discount
    # directly. The P&L shows the deduction (for transparency) and offsets
    # it in the same revenue grouping so the pair nets to $0 and no
    # downstream operator subtotal is reduced by this item.
    #
    # Stored fields (net_customer_sales, gross_profit, net_profit) are
    # left UNCHANGED — they remain settlement-real for reconciliation
    # tie-out against TikTok Seller Center. The `managed_*` properties
    # below add the offset back and are what the rendered P&L displays.
    # -----------------------------------------------------------------

    @property
    def smashbox_discount_offset(self) -> Decimal:
        """Contra credit equal in magnitude to smashbox_discount. Auto-derived
        so the offset and the deduction are always equal-and-opposite by
        construction — the pair nets to $0 on every line in every period."""
        return self.smashbox_discount

    @property
    def managed_net_customer_sales(self) -> Decimal:
        """Net Customer Sales for the rendered P&L — settlement-real value
        plus the Smashbox offset."""
        return self.net_customer_sales + self.smashbox_discount_offset

    @property
    def managed_gross_profit(self) -> Decimal:
        return self.gross_profit + self.smashbox_discount_offset

    @property
    def managed_net_profit(self) -> Decimal:
        """Net Profit for the rendered P&L. Equals net_profit computed as if
        the Smashbox-funded discount were $0 — the load-bearing invariant."""
        return self.net_profit + self.smashbox_discount_offset

    @property
    def managed_gross_margin(self) -> Decimal:
        if self.managed_net_customer_sales == 0:
            return Decimal("0")
        return self.managed_gross_profit / self.managed_net_customer_sales

    @property
    def managed_net_margin(self) -> Decimal:
        if self.managed_net_customer_sales == 0:
            return Decimal("0")
        return self.managed_net_profit / self.managed_net_customer_sales

    @property
    def managed_aov_after_discounts(self) -> Decimal:
        if self.orders_count == 0:
            return Decimal("0")
        return self.managed_net_customer_sales / Decimal(self.orders_count)

    @property
    def managed_roas(self) -> Decimal:
        """Sales per $1 net ad spend, using the managed net customer sales."""
        if self.net_ad_spend <= 0:
            return Decimal("0")
        return self.managed_net_customer_sales / self.net_ad_spend

    @property
    def managed_sales_pre_refund(self) -> Decimal:
        """Sales (TikTok-equivalent) line of the rendered P&L — includes the
        Smashbox offset. Use `sales_pre_refund` (the settlement-real
        sibling) for tie-out against TikTok Seller Center's reported sales."""
        return self.managed_net_customer_sales + self.refunds

    # Convenience aggregate for reconciliation against TikTok's reported total.
    @property
    def seller_funded_total(self) -> Decimal:
        return self.outlandish_discount + self.smashbox_discount

    @property
    def sales_pre_refund(self) -> Decimal:
        """Sales BEFORE the refund deduction — matches the headline "Sales"
        figure on TikTok Seller Center's dashboard. Net Customer Sales is the
        accounting-correct version (revenue net of returns per ASC 606); this
        sibling exists so finance can tie our numbers to what TikTok shows.

        Mathematically: gross_sales − all discounts (no refund subtraction),
        equivalently net_customer_sales + refunds.
        """
        return self.net_customer_sales + self.refunds

    @property
    def gross_margin(self) -> Decimal:
        if self.net_customer_sales == 0:
            return Decimal("0")
        return self.gross_profit / self.net_customer_sales

    @property
    def total_operating_expenses(self) -> Decimal:
        """Everything between Gross Profit and Net Profit EXCLUDING the
        TikTok Reimbursements & Adjustments line (which is Other Income,
        not an operating cost). Defined as the gap-minus-other-income so
        the math stays self-consistent: Net Profit = Gross Profit -
        Total Operating Expenses + tiktok_adjustments_net."""
        return self.gross_profit - self.net_profit + self.tiktok_adjustments_net

    @property
    def net_margin(self) -> Decimal:
        if self.net_customer_sales == 0:
            return Decimal("0")
        return self.net_profit / self.net_customer_sales

    @property
    def total_ad_spend(self) -> Decimal:
        """GROSS paid marketing in the period (before manual ad credits):
        settlement-reported Shop Ads + TikTok Ads Manager GMV Max."""
        return self.shop_ads_cost + self.gmv_max_ad_spend

    @property
    def net_ad_spend(self) -> Decimal:
        """Gross ad spend minus manually-entered TikTok ad credits. This is
        the true cash cost of marketing — what flows into the P&L and what
        ROAS is computed against."""
        return self.total_ad_spend - self.ad_credit_offset

    @property
    def roas(self) -> Decimal:
        """Return on Ad Spend: $ of Net Customer Sales generated per $1 of
        NET ad spend (after applying ad credits). 0 when no net spend
        (avoids divide-by-zero)."""
        if self.net_ad_spend <= 0:
            return Decimal("0")
        return self.net_customer_sales / self.net_ad_spend

    @property
    def gmv(self) -> Decimal:
        """TikTok Seller Center-aligned GMV for the period.

        Per TikTok's published formula:
            GMV = Price x Items + Shipping fees
                  - Seller promotions - Platform co-funding

        Mapped to our schema:
            GMV = gross_sales + shipping_revenue
                  - outlandish_discount - smashbox_discount     (seller promo)
                  - platform_discount                           (SKU platform)
                  - payment_platform_discount                   (payment platform)

        Tax excluded; refunds/cancellations NOT subtracted.

        Empirically matches Seller Center to the cent for Feb-Apr 2026 and
        within ~0.7% on May 2026 (a small classification edge with no easy
        fix from our side).

        NOTE: `payment_platform_discount` is populated by the importer on
        upload. Orders imported before the field existed show $0 until the
        orders CSV is re-uploaded — `_persist_upsert` will refresh them."""
        return (
            self.gross_sales
            + self.shipping_revenue
            - self.outlandish_discount
            - self.smashbox_discount
            - self.platform_discount
            - self.payment_platform_discount
        )


def compute_monthly_pnl(db: Session, year: int, month: int) -> MonthlyPnL:
    """Single-calendar-month P&L. Thin wrapper around compute_window_pnl."""
    start = datetime(year, month, 1)
    end = _add_month(start)
    return compute_window_pnl(db, start, end, month_anchor=start.date())


def compute_window_pnl(
    db: Session,
    start: datetime,
    end: datetime,
    *,
    month_anchor: date | None = None,
) -> MonthlyPnL:
    """P&L for an arbitrary [start, end) window.

    All lines — orders / settlements / ad spend / COGS / ad credits — are
    date-bounded against the same [start, end) window, so any range (calendar
    month, multi-month, or arbitrary day range) works directly.

    AdCredit filter: `applied_date >= start.date() AND applied_date < end.date()`
    — matching the inclusive-start / exclusive-end convention used by orders.
    A credit dated exactly on the start date is included; a credit dated on
    the exclusive-end date is not.

    `month_anchor` is the date stored on the result for display. Defaults to
    start.date(); the monthly-mode wrapper passes the first of the month.
    """
    row = db.execute(
        select(
            func.coalesce(func.sum(Order.gross_sales), 0).label("gross_sales"),
            func.coalesce(func.sum(Order.platform_discount_total), 0).label("platform_disc"),
            func.coalesce(func.sum(Order.seller_funded_outlandish), 0).label("outlandish"),
            func.coalesce(func.sum(Order.seller_funded_smashbox), 0).label("smashbox"),
            func.coalesce(func.sum(Order.payment_platform_discount), 0).label("payment_platform_disc"),
            func.coalesce(func.sum(Order.refunds), 0).label("refunds"),
            func.coalesce(func.sum(Order.tiktok_fees), 0).label("tiktok_fees"),
            func.coalesce(func.sum(Order.tiktok_referral_fee), 0).label("tiktok_referral_fee"),
            func.coalesce(func.sum(Order.tiktok_transaction_fee), 0).label("tiktok_transaction_fee"),
            func.coalesce(func.sum(Order.tiktok_refund_admin_fee), 0).label("tiktok_refund_admin_fee"),
            func.coalesce(func.sum(Order.tiktok_sales_tax_on_referral), 0).label("tiktok_sales_tax_on_referral"),
            func.coalesce(func.sum(Order.tiktok_smart_promo_fee), 0).label("tiktok_smart_promo_fee"),
            func.coalesce(func.sum(Order.tiktok_campaign_fees), 0).label("tiktok_campaign_fees"),
            func.coalesce(func.sum(Order.tiktok_partner_commission), 0).label("tiktok_partner_commission"),
            func.coalesce(func.sum(Order.tiktok_managed_service), 0).label("tiktok_managed_service"),
            func.coalesce(func.sum(Order.affiliate_commission), 0).label("affiliate_commission"),
            func.coalesce(func.sum(Order.shop_ads_cost), 0).label("shop_ads_cost"),
            func.coalesce(func.sum(Order.shipping_revenue), 0).label("ship_rev"),
            func.coalesce(func.sum(Order.shipping_cost), 0).label("ship_cost"),
            func.count(Order.id).label("orders_count"),
            # Settlement back-fill writes tiktok_fees > 0 (every settled order
            # has at least a referral fee). Use that as the settled flag.
            func.sum(case((Order.tiktok_fees > 0, 1), else_=0)).label("orders_settled"),
        )
        .where(Order.placed_at >= start, Order.placed_at < end)
        .where(Order.order_type == OrderType.PAID)
    ).one()

    cogs = _paid_cogs(db, start, end)

    gmv_max_ad_spend = Decimal(str(
        db.execute(
            select(func.coalesce(func.sum(AdSpend.amount), 0))
            .where(AdSpend.spend_date >= start, AdSpend.spend_date < end)
        ).scalar() or 0
    ))

    # AdCredit filter: same [start, end) convention as orders. Compared on the
    # Date column directly, so a credit dated on the exclusive-end day is out.
    ad_credit_offset = Decimal(str(
        db.execute(
            select(func.coalesce(func.sum(AdCredit.amount), 0))
            .where(AdCredit.applied_date >= start.date())
            .where(AdCredit.applied_date < end.date())
        ).scalar() or 0
    ))

    # TikTok settlement adjustments — logistics reimbursements, lost-package
    # credits, TikTok Shop reimbursements, bill payments, etc. Paired
    # balance/deduction rows (same adjustment_id) cancel by construction.
    # Filtered on Adjustment.create_time (when TikTok registered the entry),
    # matching the inclusive-start / exclusive-end convention used elsewhere.
    # Adjustments with null create_time are skipped (no period to attribute).
    tiktok_adjustments_net = Decimal(str(
        db.execute(
            select(func.coalesce(func.sum(Adjustment.amount), 0))
            .where(Adjustment.create_time.isnot(None))
            .where(Adjustment.create_time >= start)
            .where(Adjustment.create_time < end)
        ).scalar() or 0
    ))
    # Per-type breakdown for the expandable P&L detail. Sorted by absolute
    # value descending so the most impactful types appear first regardless
    # of sign — a $1,338 bill payment ranks above a $86 Shop reimbursement.
    type_rows = db.execute(
        select(
            Adjustment.adjustment_type,
            func.coalesce(func.sum(Adjustment.amount), 0).label("amount"),
        )
        .where(Adjustment.create_time.isnot(None))
        .where(Adjustment.create_time >= start)
        .where(Adjustment.create_time < end)
        .group_by(Adjustment.adjustment_type)
    ).all()
    tiktok_adjustments_by_type: dict[str, Decimal] = {
        r.adjustment_type: Decimal(str(r.amount))
        for r in sorted(type_rows, key=lambda r: abs(Decimal(str(r.amount))), reverse=True)
    }

    # GmvMaxReimbursement: (year, month) keyed (no date column). Determine
    # which (year, month) pairs the window touches and OR them together —
    # each touched month contributes its FULL reimbursement (no proration).
    # This is the same convention AdCredit used before it gained a date
    # column. For calendar-month windows this is a single (year, month);
    # for multi-month CUSTOM ranges every month touched sums in.
    last_y, last_m = end.year, end.month
    # If end is exactly month-start (Y-M-01 00:00:00), back up one month —
    # the boundary doesn't actually overlap M.
    if end.day == 1 and end.hour == 0 and end.minute == 0 and end.second == 0 and end.microsecond == 0:
        last_y, last_m = (last_y - 1, 12) if last_m == 1 else (last_y, last_m - 1)
    months_touched: list[tuple[int, int]] = []
    if end > start:
        yy, mm = start.year, start.month
        while (yy, mm) <= (last_y, last_m):
            months_touched.append((yy, mm))
            yy, mm = (yy + 1, 1) if mm == 12 else (yy, mm + 1)

    if months_touched:
        gmv_max_reimbursement = Decimal(str(
            db.execute(
                select(func.coalesce(func.sum(GmvMaxReimbursement.amount), 0))
                .where(or_(*[
                    and_(GmvMaxReimbursement.year == y, GmvMaxReimbursement.month == m)
                    for y, m in months_touched
                ]))
            ).scalar() or 0
        ))
    else:
        gmv_max_reimbursement = Decimal("0")

    # Sample shipping cost — captured separately from operational Shipping cost
    # (which stays PAID-only). Two channels summed:
    #   (a) Order.shipping_cost on SAMPLE / PAID_SAMPLE rows (TikTok-channel
    #       samples; populated by the settlement importer same as PAID rows).
    #   (b) Sample.shipping_cost on off-platform samples (creator seeding /
    #       agency drops; populated by app/importers/samples.py).
    # Both windowed by their respective shipping-event date (Order.placed_at
    # for TikTok-channel, Sample.shipped_at for off-platform) using the same
    # inclusive-start / exclusive-end convention as everything else.
    sample_order_shipping = Decimal(str(
        db.execute(
            select(func.coalesce(func.sum(Order.shipping_cost), 0))
            .where(Order.placed_at >= start, Order.placed_at < end)
            .where(Order.order_type.in_([OrderType.SAMPLE, OrderType.PAID_SAMPLE]))
        ).scalar() or 0
    ))
    off_platform_sample_shipping = Decimal(str(
        db.execute(
            select(func.coalesce(func.sum(Sample.shipping_cost), 0))
            .where(Sample.shipped_at >= start, Sample.shipped_at < end)
            .where(Sample.shipping_cost.isnot(None))
        ).scalar() or 0
    ))
    sample_shipping_cost = sample_order_shipping + off_platform_sample_shipping

    # Units sold (paid orders only). Bundles are one OrderLine each, so this
    # naturally counts a bundle as a single item — not its components.
    units_sold = db.execute(
        select(func.coalesce(func.sum(OrderLine.quantity), 0))
        .join(Order, Order.id == OrderLine.order_id)
        .where(Order.order_type == OrderType.PAID)
        .where(Order.placed_at >= start, Order.placed_at < end)
    ).scalar() or 0

    gross_sales = Decimal(str(row.gross_sales))
    platform_disc = Decimal(str(row.platform_disc))
    outlandish = Decimal(str(row.outlandish))
    smashbox = Decimal(str(row.smashbox))
    payment_platform_disc = Decimal(str(row.payment_platform_disc))
    refunds = Decimal(str(row.refunds))

    net_customer_sales = gross_sales - platform_disc - outlandish - smashbox - refunds
    gross_profit = net_customer_sales - cogs
    tiktok_fees = Decimal(str(row.tiktok_fees))
    affiliate = Decimal(str(row.affiliate_commission))
    shop_ads = Decimal(str(row.shop_ads_cost))
    ship_rev = Decimal(str(row.ship_rev))
    ship_cost = Decimal(str(row.ship_cost))

    net_profit = (
        gross_profit
        - tiktok_fees
        - affiliate
        - shop_ads
        - gmv_max_ad_spend
        + gmv_max_reimbursement           # Smashbox reimburses GMV Max spend
        + ad_credit_offset                # TikTok-issued credits reduce ad expense
        - ship_cost
        - sample_shipping_cost            # cash outflow for sample freight
        + ship_rev
        + tiktok_adjustments_net          # logistics/Shop reimbursements net of deductions
    )

    return MonthlyPnL(
        month=month_anchor or start.date(),
        gross_sales=gross_sales,
        platform_discount=platform_disc,
        outlandish_discount=outlandish,
        smashbox_discount=smashbox,
        payment_platform_discount=payment_platform_disc,
        refunds=refunds,
        net_customer_sales=net_customer_sales,
        cogs=cogs,
        gross_profit=gross_profit,
        tiktok_fees=tiktok_fees,
        tiktok_referral_fee=Decimal(str(row.tiktok_referral_fee)),
        tiktok_transaction_fee=Decimal(str(row.tiktok_transaction_fee)),
        tiktok_refund_admin_fee=Decimal(str(row.tiktok_refund_admin_fee)),
        tiktok_sales_tax_on_referral=Decimal(str(row.tiktok_sales_tax_on_referral)),
        tiktok_smart_promo_fee=Decimal(str(row.tiktok_smart_promo_fee)),
        tiktok_campaign_fees=Decimal(str(row.tiktok_campaign_fees)),
        tiktok_partner_commission=Decimal(str(row.tiktok_partner_commission)),
        tiktok_managed_service=Decimal(str(row.tiktok_managed_service)),
        affiliate_commission=affiliate,
        shop_ads_cost=shop_ads,
        gmv_max_ad_spend=gmv_max_ad_spend,
        gmv_max_reimbursement=gmv_max_reimbursement,
        ad_credit_offset=ad_credit_offset,
        shipping_revenue=ship_rev,
        shipping_cost=ship_cost,
        sample_shipping_cost=sample_shipping_cost,
        tiktok_adjustments_net=tiktok_adjustments_net,
        net_profit=net_profit,
        tiktok_adjustments_by_type=tiktok_adjustments_by_type,
        orders_count=int(row.orders_count or 0),
        orders_settled=int(row.orders_settled or 0),
        units_sold=int(units_sold),
    )


def _paid_cogs(db: Session, start: datetime, end: datetime) -> Decimal:
    """Sum qty * unit_cogs_snapshot for paid orders. Falls back to SKU master
    COGS when the snapshot is zero (legacy rows imported before COGS was set)."""
    # OrderLine.sku holds the TikTok SKU ID after resolution. Fallback joins
    # against Sku.tiktok_sku_id for SKUs that exist in the master but somehow
    # missed snapshotting. Bundles are not in this fallback — they always have
    # a populated snapshot from the resolver.
    stmt = (
        select(
            func.coalesce(
                func.sum(
                    OrderLine.quantity
                    * func.coalesce(
                        func.nullif(OrderLine.unit_cogs_snapshot, 0),
                        func.coalesce(Sku.unit_cogs, 0),
                    )
                ),
                0,
            )
        )
        .select_from(OrderLine)
        .join(Order, Order.id == OrderLine.order_id)
        .join(Sku, Sku.tiktok_sku_id == OrderLine.sku, isouter=True)
        .where(Order.order_type == OrderType.PAID)
        .where(Order.placed_at >= start, Order.placed_at < end)
    )
    return Decimal(str(db.execute(stmt).scalar() or 0))


def _add_month(d: datetime) -> datetime:
    if d.month == 12:
        return datetime(d.year + 1, 1, 1)
    return datetime(d.year, d.month + 1, 1)
