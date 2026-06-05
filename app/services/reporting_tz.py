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

Only TikTok timestamps (`placed_at`, and `Adjustment.create_time`) get this
treatment. User-entered dates (`AdCredit.applied_date`) and monthly ad-spend
dates (`AdSpend.spend_date`) are calendar values and must NOT be shifted.

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


def shop_boundary_to_source(boundary: datetime) -> datetime:
    """Convert a shop-local calendar boundary (naive) to the equivalent naive
    timestamp in placed_at's source zone, for direct comparison against
    `Order.placed_at` in a half-open [start, end) window filter."""
    return boundary.replace(tzinfo=SHOP_TZ).astimezone(SOURCE_TZ).replace(tzinfo=None)


def today_local() -> date:
    """Today's date in the shop's timezone — for 'current period' defaults so the
    dashboard / reports roll over at shop-local (not server/UTC) midnight."""
    return datetime.now(SHOP_TZ).date()
