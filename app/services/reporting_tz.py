"""Shop-local-time bucketing for reports, so figures tie to TikTok Seller Center.

TikTok Seller Center buckets each order by the shop's local (Pacific) day. Our
`Order.placed_at` is the raw TikTok "Created Time", stored naive. Empirically it
sits in a fixed UTC-6 offset: converting placed_at to America/Los_Angeles and
bucketing by that local day cuts daily variance vs Seller Center ~90-97% for
Mar-May 2026 (validated 2026-06 against TikTokDailyMetric on prod — reproduces
and extends the original 2026-05-19 March-only -1h finding).

We convert at query/boundary time. Stored timestamps are NEVER rewritten, so the
change is reversible and `placed_at` keeps its exact-from-TikTok provenance.

Two directions:
  - `placed_local_date(dt)` — the shop-local day a placed_at falls on. Used for
    per-row daily bucketing (reconciliation), where a single month can straddle
    the DST boundary so a constant offset would be wrong.
  - `shop_boundary_to_source(boundary)` — convert a shop-local calendar boundary
    (e.g. datetime(2026, 3, 1)) into the equivalent naive source-zone timestamp,
    so a `placed_at >= start AND < end` window filter selects the orders Seller
    Center would bucket into that period. Index-friendly (no per-row SQL tz math).

Scope: only `Order.placed_at` (order Created Time) gets this treatment — that
is the one stream empirically validated against the Seller Center daily Sales
tile. User-entered dates (`AdCredit.applied_date`), monthly ad-spend dates
(`AdSpend.spend_date`), the settlement adjustment feed (`Adjustment.create_time`),
and the internal forecasting/velocity series are deliberately left on their raw
timestamps: adjustments are a separate settlement-level stream with no validated
offset, and velocity/demand-planning don't reconcile against Seller Center (it
reports no such figure), so shifting them adds boundary fragility for no tie-out.

CAVEAT — re-validate on winter data: the UTC-6 source model was validated only
against PDT-season months (Mar-May). The winter PST behavior (-2h) is inferred
from the fixed-offset model, not measured. Re-run the offset sweep once Nov-Feb
order + TikTokDailyMetric data exists.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

# `placed_at`'s implied source zone. POSIX `Etc/GMT+6` == UTC-6 (sign inverted).
SOURCE_TZ = ZoneInfo("Etc/GMT+6")
# The shop's reporting timezone (Shop.timezone). Seller Center buckets in this.
SHOP_TZ = ZoneInfo("America/Los_Angeles")


def placed_local(placed_at: datetime) -> datetime:
    """A raw placed_at (naive, source zone) as a shop-local *aware* datetime."""
    return placed_at.replace(tzinfo=SOURCE_TZ).astimezone(SHOP_TZ)


def placed_local_date(placed_at: datetime) -> date:
    """The shop-local calendar day a placed_at falls on (its Seller Center day)."""
    return placed_local(placed_at).date()


def shop_boundary_to_source(boundary: datetime | date) -> datetime:
    """Convert a shop-local calendar boundary (naive) to the equivalent naive
    timestamp in placed_at's source zone, for direct comparison against
    `Order.placed_at` in a half-open [start, end) window filter.

    Accepts a plain `date` too (custom-period selectors pass dates) — it's
    promoted to midnight before conversion."""
    if not isinstance(boundary, datetime):
        boundary = datetime(boundary.year, boundary.month, boundary.day)
    return boundary.replace(tzinfo=SHOP_TZ).astimezone(SOURCE_TZ).replace(tzinfo=None)


def placed_window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """Convert a shop-local calendar window [start, end) into the source-zone
    boundaries to filter `Order.placed_at` (and other TikTok timestamps like
    `Adjustment.create_time`) against. Convenience wrapper around
    `shop_boundary_to_source` for the common two-boundary case."""
    return shop_boundary_to_source(start), shop_boundary_to_source(end)


def today_local() -> date:
    """Today's date in the shop's timezone — for 'current period' defaults so the
    dashboard / reports roll over at shop-local (not server/UTC) midnight."""
    return datetime.now(SHOP_TZ).date()


def now_local() -> datetime:
    """The current shop-local wall-clock time, naive (tz stripped). For 'as of
    now' forecasting anchors and current-period defaults that need a datetime,
    so they roll at shop-local midnight rather than server/UTC midnight. Pairs
    with `today_local()` (which returns just the date)."""
    return datetime.now(SHOP_TZ).replace(tzinfo=None)
