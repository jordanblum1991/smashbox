"""Tests for the P&L custom date range feature.

Four areas:
  1. CRITICAL regression guard: compute_window_pnl over a single calendar
     month must match compute_monthly_pnl field-for-field — proves the
     refactor (extracting compute_window_pnl + month version delegates) did
     not change month semantics.
  2. Custom-range windows include AdCredits by `applied_date`, with the
     same inclusive-start / exclusive-end convention as orders.
  3. Cross-month windows pull in only the credits whose dates fall inside
     the window — NOT every month's full credit (the old behavior).
  4. compute_pnl_view(CUSTOM) input validation + route error redirect.
"""
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.db import Base, SessionLocal, engine
from app.models import (
    AdCredit,
    AdSpend,
    ImportBatch,
    ImportBatchStatus,
    ImportFileKind,
    Order,
    OrderLine,
    OrderType,
    Sample,
)
from app.reports.monthly_pnl import compute_monthly_pnl, compute_window_pnl
from app.reports.pnl import PeriodKind, compute_pnl_view
from app.routers.reports import pnl_view


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _batch(db) -> ImportBatch:
    b = ImportBatch(
        kind=ImportFileKind.TIKTOK_ORDERS,
        status=ImportBatchStatus.COMPLETED,
        original_filename="t.csv",
        stored_path="/tmp/t.csv",
    )
    db.add(b)
    db.flush()
    return b


def _paid_order(
    db,
    batch_id: int,
    placed_at: datetime,
    *,
    tt_id: str,
    gross_sales: Decimal = Decimal("100.00"),
    platform_discount: Decimal = Decimal("0"),
    outlandish: Decimal = Decimal("0"),
    smashbox: Decimal = Decimal("0"),
    refunds: Decimal = Decimal("0"),
    tiktok_fees: Decimal = Decimal("10.00"),
    affiliate: Decimal = Decimal("0"),
    shop_ads: Decimal = Decimal("0"),
    ship_rev: Decimal = Decimal("0"),
    ship_cost: Decimal = Decimal("0"),
    units: int = 1,
    sku: str = "SBX-001",
    unit_cogs_snapshot: Decimal = Decimal("3.00"),
) -> Order:
    """Seed a PAID order + one OrderLine with controllable totals."""
    order = Order(
        import_batch_id=batch_id,
        tiktok_order_id=tt_id,
        placed_at=placed_at,
        order_type=OrderType.PAID,
        status="Shipped",
        brand="smashbox",
        gross_sales=gross_sales,
        platform_discount_total=platform_discount,
        seller_funded_outlandish=outlandish,
        seller_funded_smashbox=smashbox,
        refunds=refunds,
        tiktok_fees=tiktok_fees,
        tiktok_referral_fee=tiktok_fees,
        affiliate_commission=affiliate,
        shop_ads_cost=shop_ads,
        shipping_revenue=ship_rev,
        shipping_cost=ship_cost,
    )
    db.add(order)
    db.flush()
    db.add(OrderLine(
        order_id=order.id,
        sku=sku,
        quantity=units,
        unit_price=gross_sales / units if units else gross_sales,
        gross_sales=gross_sales,
        unit_cogs_snapshot=unit_cogs_snapshot,
    ))
    db.flush()
    return order


def _ad_credit(year: int, month: int, day: int, amount: str, note: str | None = None) -> AdCredit:
    """Construct an AdCredit with both `applied_date` and the derived
    (year, month) pair — matches what the route does on a real write."""
    return AdCredit(
        year=year, month=month,
        applied_date=date(year, month, day),
        amount=Decimal(amount),
        note=note,
    )


def _seed_march_and_april(db) -> int:
    """Seed a varied set of paid orders + AdCredits + AdSpend across March
    and April 2026. Returns the batch_id used.

    Credits are dated MID-MONTH so the cross-month window tests have
    something interesting to assert against (a Mar 15 credit is excluded
    from a Mar 28+ window; an Apr 10 credit is included)."""
    batch = _batch(db)
    bid = batch.id

    # March orders — early, mid, late
    _paid_order(db, bid, datetime(2026, 3, 2, 9), tt_id="TT-M-1",
                gross_sales=Decimal("120.00"), tiktok_fees=Decimal("12.00"), units=2)
    _paid_order(db, bid, datetime(2026, 3, 15, 14), tt_id="TT-M-2",
                gross_sales=Decimal("80.00"), tiktok_fees=Decimal("8.00"), units=1)
    _paid_order(db, bid, datetime(2026, 3, 29, 11), tt_id="TT-M-3",
                gross_sales=Decimal("200.00"), platform_discount=Decimal("20.00"),
                outlandish=Decimal("8.00"), smashbox=Decimal("12.00"),
                tiktok_fees=Decimal("20.00"), units=3)

    # April orders — early, mid, late
    _paid_order(db, bid, datetime(2026, 4, 5, 8), tt_id="TT-A-1",
                gross_sales=Decimal("150.00"), tiktok_fees=Decimal("15.00"), units=1)
    _paid_order(db, bid, datetime(2026, 4, 20, 16), tt_id="TT-A-2",
                gross_sales=Decimal("90.00"), refunds=Decimal("10.00"),
                tiktok_fees=Decimal("9.00"), units=1)

    # AdCredits — dated mid-month.
    db.add(_ad_credit(2026, 3, 15, "100.00", "Mar credit"))
    db.add(_ad_credit(2026, 4, 10, "200.00", "Apr credit"))

    # AdSpend — a row in each month so gmv_max_ad_spend has data to filter.
    db.add(AdSpend(import_batch_id=bid, spend_date=datetime(2026, 3, 10),
                   campaign_id="C1", amount=Decimal("50.00")))
    db.add(AdSpend(import_batch_id=bid, spend_date=datetime(2026, 4, 10),
                   campaign_id="C2", amount=Decimal("75.00")))

    db.commit()
    return bid


# Fields that must match byte-for-byte between the month wrapper and the
# window primitive when the window is exactly a calendar month.
_MATCH_FIELDS = (
    "gross_sales", "platform_discount", "outlandish_discount",
    "smashbox_discount", "refunds", "net_customer_sales",
    "cogs", "gross_profit",
    "tiktok_fees", "tiktok_referral_fee", "tiktok_transaction_fee",
    "tiktok_refund_admin_fee", "tiktok_sales_tax_on_referral",
    "tiktok_smart_promo_fee", "tiktok_campaign_fees",
    "tiktok_partner_commission", "tiktok_managed_service",
    "affiliate_commission", "shop_ads_cost",
    "gmv_max_ad_spend", "ad_credit_offset",
    "shipping_revenue", "shipping_cost", "sample_shipping_cost",
    "net_profit",
    "orders_count", "orders_settled", "units_sold",
)


# ---------------------------------------------------------------------------
# 1. REGRESSION GUARD — single-month window matches the legacy wrapper exactly
# ---------------------------------------------------------------------------

def test_compute_window_pnl_matches_compute_monthly_pnl_for_march():
    """compute_monthly_pnl(2026, 3) is a thin wrapper around compute_window_pnl.
    If this fails, monthly P&L numbers shift. Every field must match."""
    with SessionLocal() as db:
        _seed_march_and_april(db)

    with SessionLocal() as db:
        legacy = compute_monthly_pnl(db, 2026, 3)
        windowed = compute_window_pnl(
            db,
            datetime(2026, 3, 1),
            datetime(2026, 4, 1),
            month_anchor=date(2026, 3, 1),
        )

    for field in _MATCH_FIELDS:
        assert getattr(legacy, field) == getattr(windowed, field), (
            f"{field}: legacy={getattr(legacy, field)} vs windowed={getattr(windowed, field)}"
        )
    assert legacy.month == windowed.month


def test_compute_window_pnl_matches_compute_monthly_pnl_for_april():
    with SessionLocal() as db:
        _seed_march_and_april(db)

    with SessionLocal() as db:
        legacy = compute_monthly_pnl(db, 2026, 4)
        windowed = compute_window_pnl(
            db,
            datetime(2026, 4, 1),
            datetime(2026, 5, 1),
            month_anchor=date(2026, 4, 1),
        )

    for field in _MATCH_FIELDS:
        assert getattr(legacy, field) == getattr(windowed, field), (
            f"{field}: legacy={getattr(legacy, field)} vs windowed={getattr(windowed, field)}"
        )


def test_monthly_pnl_includes_credit_dated_mid_month():
    """A mid-month credit (Mar 15) must still land in the March monthly P&L."""
    with SessionLocal() as db:
        _seed_march_and_april(db)
    with SessionLocal() as db:
        march = compute_monthly_pnl(db, 2026, 3)
    assert march.ad_credit_offset == Decimal("100.00")


# ---------------------------------------------------------------------------
# 2. CROSS-MONTH — credits are filtered by date, NOT by "every month touched"
# ---------------------------------------------------------------------------

def test_cross_month_window_includes_only_credits_inside_window():
    """Mar 28 - Apr 27 spans two calendar months. The Mar 15 credit is BEFORE
    the window (so excluded); the Apr 10 credit is INSIDE (so included).
    Under the old month-touched logic this would have been $300; under
    date-granularity it's $200."""
    with SessionLocal() as db:
        _seed_march_and_april(db)

    with SessionLocal() as db:
        start = datetime(2026, 3, 28)
        end = datetime(2026, 4, 28)              # exclusive — covers all of Apr 27
        view = compute_window_pnl(db, start, end)

    assert view.ad_credit_offset == Decimal("200.00"), (
        "Only the April 10 credit falls inside Mar 28 – Apr 27; "
        "the March 15 credit is before the window."
    )
    # Order-level sums in the window: TT-M-3 (Mar 29) + TT-A-1 (Apr 5) + TT-A-2 (Apr 20).
    assert view.orders_count == 3
    assert view.gross_sales == Decimal("440.00")


def test_cross_month_window_includes_credits_from_both_months_when_dates_fit():
    """When BOTH credit dates fall inside the window, both are summed.
    Mar 10 – Apr 30: Mar 15 ($100) + Apr 10 ($200) = $300."""
    with SessionLocal() as db:
        _seed_march_and_april(db)
    with SessionLocal() as db:
        view = compute_window_pnl(
            db, datetime(2026, 3, 10), datetime(2026, 4, 30)
        )
    assert view.ad_credit_offset == Decimal("300.00")


def test_cross_month_window_excludes_orders_outside_dates():
    """Tight 3-day window — only Mar 15 order (TT-M-2) should count."""
    with SessionLocal() as db:
        _seed_march_and_april(db)
    with SessionLocal() as db:
        view = compute_window_pnl(
            db, datetime(2026, 3, 14), datetime(2026, 3, 17)
        )
    assert view.orders_count == 1
    assert view.gross_sales == Decimal("80.00")


# ---------------------------------------------------------------------------
# 3. Boundary tests for the new date-bounded AdCredit filter
# ---------------------------------------------------------------------------

def _seed_just_one_credit(db, *, day: int) -> None:
    """Seed a single AdCredit on 2026-03-<day> for boundary testing."""
    db.add(_ad_credit(2026, 3, day, "100.00", "boundary test"))
    db.commit()


def test_credit_on_exact_window_start_is_included():
    """Inclusive start: a credit dated 2026-03-15 IS in a window starting
    2026-03-15 00:00. Matches Order.placed_at >= start convention."""
    with SessionLocal() as db:
        _seed_just_one_credit(db, day=15)
    with SessionLocal() as db:
        view = compute_window_pnl(
            db, datetime(2026, 3, 15), datetime(2026, 3, 20)
        )
    assert view.ad_credit_offset == Decimal("100.00")


def test_credit_on_exclusive_end_day_is_excluded():
    """Exclusive end: a credit dated 2026-03-20 is NOT in a window that ends
    at midnight 2026-03-20 (the credit's day is one past the last included
    day). Matches Order.placed_at < end convention."""
    with SessionLocal() as db:
        _seed_just_one_credit(db, day=20)
    with SessionLocal() as db:
        view = compute_window_pnl(
            db, datetime(2026, 3, 10), datetime(2026, 3, 20)
        )
    assert view.ad_credit_offset == Decimal("0")


def test_credit_just_before_window_start_is_excluded():
    """Credit dated one day before start → excluded."""
    with SessionLocal() as db:
        _seed_just_one_credit(db, day=14)
    with SessionLocal() as db:
        view = compute_window_pnl(
            db, datetime(2026, 3, 15), datetime(2026, 3, 20)
        )
    assert view.ad_credit_offset == Decimal("0")


def test_credit_in_middle_of_window_is_included():
    """Credit strictly inside the window → included."""
    with SessionLocal() as db:
        _seed_just_one_credit(db, day=17)
    with SessionLocal() as db:
        view = compute_window_pnl(
            db, datetime(2026, 3, 15), datetime(2026, 3, 20)
        )
    assert view.ad_credit_offset == Decimal("100.00")


def test_custom_mode_route_pulls_credit_by_applied_date():
    """End-to-end: compute_pnl_view(CUSTOM) with an inclusive end-date of
    Mar 19 (route bumps to exclusive Mar 20) includes a credit dated Mar 17."""
    with SessionLocal() as db:
        _seed_just_one_credit(db, day=17)
    with SessionLocal() as db:
        view = compute_pnl_view(
            db, PeriodKind.CUSTOM,
            start_date=date(2026, 3, 15), end_date=date(2026, 3, 19),
        )
    assert view.total.ad_credit_offset == Decimal("100.00")


# ---------------------------------------------------------------------------
# 4. Input validation — compute_pnl_view + route redirect
# ---------------------------------------------------------------------------

def test_compute_pnl_view_custom_rejects_inverted_dates():
    with SessionLocal() as db:
        with pytest.raises(ValueError, match="start_date must be <= end_date"):
            compute_pnl_view(
                db, PeriodKind.CUSTOM,
                start_date=date(2026, 4, 27),
                end_date=date(2026, 3, 28),
            )


def test_compute_pnl_view_custom_rejects_missing_dates():
    with SessionLocal() as db:
        with pytest.raises(ValueError, match="requires both start_date and end_date"):
            compute_pnl_view(db, PeriodKind.CUSTOM)


def test_route_redirects_on_inverted_dates():
    with SessionLocal() as db:
        resp = pnl_view(
            request=None,
            period=PeriodKind.CUSTOM,
            start_date="2026-04-27",
            end_date="2026-03-28",
            db=db,
        )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]
    assert "Start+date+must+be" in resp.headers["location"] or \
           "Start%20date%20must%20be" in resp.headers["location"]


def test_route_redirects_on_missing_dates():
    with SessionLocal() as db:
        resp = pnl_view(
            request=None,
            period=PeriodKind.CUSTOM,
            start_date=None,
            end_date=None,
            db=db,
        )
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]


def test_route_redirects_on_garbage_date():
    with SessionLocal() as db:
        resp = pnl_view(
            request=None,
            period=PeriodKind.CUSTOM,
            start_date="not-a-date",
            end_date="2026-04-01",
            db=db,
        )
    assert resp.status_code == 303
    assert "Invalid+date+format" in resp.headers["location"] or \
           "Invalid%20date%20format" in resp.headers["location"]


# ---------------------------------------------------------------------------
# 5. Sample shipping cost — separate line, both channels
# ---------------------------------------------------------------------------

def _sample_order(
    db,
    batch_id: int,
    placed_at: datetime,
    *,
    tt_id: str,
    order_type: OrderType,
    shipping_cost: Decimal,
) -> Order:
    """SAMPLE / PAID_SAMPLE order with a known shipping_cost. Mirrors the
    settlement-importer back-fill that populates Order.shipping_cost on
    every order regardless of type."""
    order = Order(
        import_batch_id=batch_id,
        tiktok_order_id=tt_id,
        placed_at=placed_at,
        order_type=order_type,
        status="Shipped",
        brand="smashbox",
        gross_sales=Decimal("0"),  # samples have $0 gross by the detection rule
        shipping_cost=shipping_cost,
    )
    db.add(order)
    db.flush()
    return order


def _off_platform_sample(
    db,
    batch_id: int,
    shipped_at: datetime,
    *,
    sku: str = "SBX-001",
    shipping_cost: Decimal | None,
) -> Sample:
    s = Sample(
        import_batch_id=batch_id,
        shipped_at=shipped_at,
        sku=sku,
        quantity=1,
        shipping_cost=shipping_cost,
    )
    db.add(s)
    db.flush()
    return s


def test_sample_shipping_zero_when_only_paid_orders():
    """Baseline: with only PAID orders, sample_shipping_cost is $0 and
    shipping_cost is unchanged from the legacy behaviour."""
    with SessionLocal() as db:
        b = _batch(db)
        _paid_order(db, b.id, datetime(2026, 3, 10), tt_id="P1",
                    ship_cost=Decimal("5.00"))
        db.commit()

    with SessionLocal() as db:
        pnl = compute_monthly_pnl(db, 2026, 3)

    assert pnl.shipping_cost == Decimal("5.00")
    assert pnl.sample_shipping_cost == Decimal("0")


def test_sample_shipping_captures_sample_order_shipping_only():
    """Mixed PAID + SAMPLE/PAID_SAMPLE: shipping_cost stays PAID-only,
    sample_shipping_cost captures the sample-order shipping sum."""
    with SessionLocal() as db:
        b = _batch(db)
        _paid_order(db, b.id, datetime(2026, 3, 5), tt_id="P-A",
                    ship_cost=Decimal("4.00"))
        _sample_order(db, b.id, datetime(2026, 3, 7), tt_id="S-A",
                      order_type=OrderType.SAMPLE,
                      shipping_cost=Decimal("3.50"))
        _sample_order(db, b.id, datetime(2026, 3, 14), tt_id="S-B",
                      order_type=OrderType.PAID_SAMPLE,
                      shipping_cost=Decimal("6.25"))
        db.commit()

    with SessionLocal() as db:
        pnl = compute_monthly_pnl(db, 2026, 3)

    assert pnl.shipping_cost == Decimal("4.00"), "Shipping cost must remain PAID-only"
    assert pnl.sample_shipping_cost == Decimal("9.75")  # 3.50 + 6.25


def test_sample_shipping_captures_off_platform_sample_rows():
    """Off-platform Sample rows with shipping_cost populated must flow into
    sample_shipping_cost. The PAID Shipping cost line is unaffected."""
    with SessionLocal() as db:
        b = _batch(db)
        _paid_order(db, b.id, datetime(2026, 3, 10), tt_id="P-X",
                    ship_cost=Decimal("2.00"))
        _off_platform_sample(db, b.id, datetime(2026, 3, 12),
                             shipping_cost=Decimal("8.00"))
        _off_platform_sample(db, b.id, datetime(2026, 3, 20),
                             shipping_cost=Decimal("4.50"))
        db.commit()

    with SessionLocal() as db:
        pnl = compute_monthly_pnl(db, 2026, 3)

    assert pnl.shipping_cost == Decimal("2.00")
    assert pnl.sample_shipping_cost == Decimal("12.50")  # 8.00 + 4.50


def test_sample_shipping_combines_both_channels_and_reduces_net_profit():
    """All three sources present: PAID order shipping, sample-order shipping,
    and off-platform Sample shipping. The two P&L lines partition cleanly,
    and net_profit reflects the additional sample shipping deduction."""
    with SessionLocal() as db:
        b = _batch(db)
        _paid_order(db, b.id, datetime(2026, 3, 5), tt_id="P-C",
                    gross_sales=Decimal("100.00"),
                    tiktok_fees=Decimal("0"),
                    ship_cost=Decimal("4.00"),
                    unit_cogs_snapshot=Decimal("0"))
        _sample_order(db, b.id, datetime(2026, 3, 10), tt_id="S-C",
                      order_type=OrderType.SAMPLE,
                      shipping_cost=Decimal("3.00"))
        _off_platform_sample(db, b.id, datetime(2026, 3, 15),
                             shipping_cost=Decimal("5.00"))
        db.commit()

    with SessionLocal() as db:
        pnl = compute_monthly_pnl(db, 2026, 3)

    assert pnl.shipping_cost == Decimal("4.00")
    assert pnl.sample_shipping_cost == Decimal("8.00")  # 3.00 + 5.00
    # net_profit must deduct BOTH; with gross 100, no fees, no COGS, no ad
    # spend, net = 100 - 4 - 8 = 88.
    assert pnl.net_profit == Decimal("88.00")


def test_sample_shipping_ignores_null_sample_shipping_cost():
    """Sample rows with NULL shipping_cost must be silently skipped — no
    sum contribution, no error. Mirrors how the data arrives when a row
    pre-dates the cost-tracking column or simply wasn't recorded."""
    with SessionLocal() as db:
        b = _batch(db)
        _off_platform_sample(db, b.id, datetime(2026, 3, 10),
                             shipping_cost=None)
        _off_platform_sample(db, b.id, datetime(2026, 3, 11),
                             shipping_cost=Decimal("4.00"))
        _off_platform_sample(db, b.id, datetime(2026, 3, 12),
                             shipping_cost=None)
        db.commit()

    with SessionLocal() as db:
        pnl = compute_monthly_pnl(db, 2026, 3)

    assert pnl.sample_shipping_cost == Decimal("4.00")


def test_sample_shipping_period_boundary_attribution():
    """A SAMPLE Order on the last instant before month-end belongs to March;
    a Sample shipped at midnight on the 1st of April belongs to April.
    Confirms the same inclusive-start / exclusive-end convention as orders."""
    with SessionLocal() as db:
        b = _batch(db)
        # On the last second of March — must be IN March.
        _sample_order(db, b.id, datetime(2026, 3, 31, 23, 59, 59), tt_id="S-LAST",
                      order_type=OrderType.SAMPLE,
                      shipping_cost=Decimal("7.00"))
        # Exactly month-start of April — must be IN April, NOT March.
        _off_platform_sample(db, b.id, datetime(2026, 4, 1, 0, 0, 0),
                             shipping_cost=Decimal("11.00"))
        db.commit()

    with SessionLocal() as db:
        march = compute_monthly_pnl(db, 2026, 3)
        april = compute_monthly_pnl(db, 2026, 4)

    assert march.sample_shipping_cost == Decimal("7.00"), \
        "March 31 23:59:59 sample-order shipping belongs to March"
    assert april.sample_shipping_cost == Decimal("11.00"), \
        "April 1 00:00:00 off-platform sample shipping belongs to April, not March"
