# Temporal Patterns (Timing tab — Phase 2 of SKU Sales Analytics) — Design

**Date:** 2026-06-22
**Status:** Approved (design)

## Context

The SKU Sales Analytics work is decomposed into three phases on `/reports/sales`:
1. **SKU Performance** (shipped) — per-SKU units/sales, momentum, lifecycle.
2. **Temporal patterns (this spec)** — *aggregate* time-of-sale patterns.
3. SKU × time heatmap — *future*.

The tab bar already has **Overview · SKUs · Timing**, with **Timing** rendered as a
disabled "soon" chip. This spec fills it in. The tab is **aggregate-only** (no
per-SKU breakdown — that's Phase 3).

Data is available: `Order.placed_at` (full timestamp) → `reporting_tz.placed_local()`
gives the shop-local datetime (hence local **weekday** and **hour**). PAID orders only,
consistent with the rest of the Sales page.

## Decisions (from brainstorming)

- **Metric: Revenue** (single metric, no toggle). Uses the **velocity report's
  canonical GMV** per order so totals reconcile:
  `gross_sales + shipping_revenue − seller_funded_outlandish − seller_funded_smashbox
  − platform_discount_total − payment_platform_discount`.
- **Time-of-day: 24 hourly bars** (12a→11p), peak hour highlighted, with a small
  Morning/Afternoon/Evening/Night daypart note for context.
- **Insight callouts: yes** — short auto-generated takeaways alongside the charts.
- **Day-of-week measured as average revenue per occurrence** (not raw total), so a
  window with uneven weekday counts (5 Mondays vs 4 Sundays) compares fairly.

## Architecture

### 1. Report module — `app/reports/temporal_patterns.py` (new)

Pure computation (reads ORM, returns dataclasses), mirroring `sku_performance.py`.

```python
@dataclass
class DowStat:
    weekday: int          # 0=Mon … 6=Sun
    label: str            # "Mon" … "Sun"
    revenue: Decimal      # total revenue on that weekday across the window
    occurrences: int      # how many of that weekday fall in the window
    avg_revenue: Decimal  # revenue / occurrences (the comparison metric)
    is_peak: bool

@dataclass
class HourStat:
    hour: int             # 0..23 (shop-local)
    label: str            # "12a", "1a", … "11p"
    revenue: Decimal
    is_peak: bool

@dataclass
class DaypartStat:
    key: str              # "morning" | "afternoon" | "evening" | "night"
    label: str            # "Morning" …
    revenue: Decimal
    is_peak: bool

@dataclass
class DayStat:
    day: date
    label: str            # "May 24"
    revenue: Decimal

@dataclass
class TrendShape:
    has_enough: bool      # >= MIN_TREND_DAYS days AND total_revenue > 0
    label: str            # "Steady" | "Spiky" | "Trending up" | "Trending down"
                          #   or "Not enough data" when has_enough is False
    detail: str           # e.g. "2nd half +22% vs 1st; fairly even day-to-day"
    direction: str        # "up" | "down" | "flat" | "na"
    volatility: str       # "spiky" | "steady" | "na"

@dataclass
class TemporalInsights:
    strongest_dow: DowStat | None
    strongest_dow_pct: Decimal | None   # avg_revenue vs the window's daily average
    peak_hour: HourStat | None
    best_day: DayStat | None            # top single calendar day
    trend: TrendShape

@dataclass
class TemporalView:
    dow: list[DowStat]          # 7, Mon..Sun
    hours: list[HourStat]       # 24, 0..23
    dayparts: list[DaypartStat] # 4
    daily: list[DayStat]        # every day in [start, end], zero-filled (trend chart)
    top_days: list[DayStat]     # top 3 by revenue (revenue > 0)
    insights: TemporalInsights
    total_revenue: Decimal
    window_start: date
    window_end: date

def compute_temporal_patterns(db, *, start, end) -> TemporalView
```

**Aggregation (PAID only):** select per-order `(placed_at, <revenue expr>)` for orders
placed in the shop-local window (source-tz bounds via `placed_window`, same as the SKU
report). For each row compute `local = placed_local(placed_at)` and accumulate revenue
into `dow[local.weekday()]`, `hours[local.hour]`, and `daily[local.date()]`.

**Day-of-week occurrences:** counted from the calendar window (`[start, end]`), so
`avg_revenue = revenue / occurrences` is defined even for weekdays with zero sales.
`is_peak` marks the max-`avg_revenue` weekday.

**Dayparts:** Morning 05:00–11:59 · Afternoon 12:00–16:59 · Evening 17:00–21:59 ·
Night 22:00–04:59. Derived by summing the relevant `hours`.

**Trend shape** (over the zero-filled `daily` revenue series, `n` days):
- `has_enough = n >= MIN_TREND_DAYS (8) and total_revenue > 0`; else label
  "Not enough data", direction/volatility "na".
- Direction: split into first/second half (`mid = n // 2`); `pct = (avg2 − avg1)/avg1`.
  `> +15%` → "up", `< −15%` → "down", else "flat".
- Volatility: coefficient of variation `cv = pstdev(series) / mean(series)`;
  `> 0.6` → "spiky", else "steady".
- Headline `label`: direction wins — up → "Trending up", down → "Trending down";
  else spiky → "Spiky"; else "Steady". `detail` states the actual pct + evenness.

**Insights:** `strongest_dow` = max `avg_revenue`; `strongest_dow_pct` = that avg vs
the window's daily average (`total_revenue / days_in_window`); `peak_hour` = max-revenue
hour; `best_day` = `top_days[0]`; `trend` = the `TrendShape`.

Thresholds (`MIN_TREND_DAYS=8`, direction ±15%, CV 0.6) are module constants, tunable.

### 2. Route — extend `sales_view` (`app/routers/reports.py`)

Add a `tab == "timing"` branch: resolve the window exactly as today (the existing
`_sales_view_data` yields `view.window_start/window_end` for every scope), then call
`compute_temporal_patterns(db, start=…, end=…)` and add `temporal` to the context.
`ctx["tab"]` normalization extends to accept `"timing"`. Overview/SKUs and the CSV
route are unchanged. No pagination (aggregate view).

### 3. Template — `app/templates/reports/sales.html`

- **Enable the Timing tab:** replace the disabled "soon" `<span>` with a real
  `<a href="/reports/sales?{{ period_qs }}&tab=timing">` (active-state styled like the
  others).
- **`{% if tab == 'timing' %}` block:**
  - **Insight callouts** — cards: 🗓️ strongest weekday (label + "+N% vs daily avg"),
    ⏰ peak hour (e.g. "12–1pm"), 📈 trend (the shape label), 🔥 best single day.
  - **Day-of-week panel** — Mon→Sun bars (`avg_revenue`) via the existing `ui.barchart`,
    peak bar highlighted, weekday labels beneath.
  - **Time-of-day panel** — 24 hourly revenue bars via `ui.barchart`, peak hour
    highlighted, with the 4-daypart summary line beneath.
  - **Trend / arc panel** — the daily revenue series as a bar chart + the shape label +
    `detail` sentence + the top-3 strongest-days list.
- Reuses `ui.barchart` / `sparkline` and the existing card/section styling — no new
  chart primitives. Mobile: charts are inline-SVG (responsive); callouts in a
  `grid-cols-2 sm:grid-cols-4` grid; panels stack.

## Data flow

```
/reports/sales?tab=timing&<period params>
  → resolve window (existing _sales_view_data) → window_start/_end
  → compute_temporal_patterns(db, start, end)
       per-order revenue → placed_local → weekday/hour/date buckets;
       dow avg-per-occurrence; dayparts; trend shape; insights
  → render sales.html Timing tab (callouts + 3 chart panels)
```

## Error handling / edge cases

- **Empty window** (no PAID revenue): all bars zero, callouts show "—", `top_days`
  empty, trend "Not enough data". No crash, no divide-by-zero (guard `occurrences`,
  `days_in_window`, `mean`, `avg1` before dividing).
- **< 8 days** (e.g. a short custom range or fiscal_month early in the period): charts
  still render; trend shows "Not enough data for a trend."
- **Single-day window**: DOW has one weekday with occurrences=1; hours render; trend
  suppressed.
- Revenue is `Decimal`, quantized to cents; never float. (CV/mean use float internally
  for the ratio only; the displayed revenue stays Decimal.)
- Invalid `tab` → falls back to "overview" (existing behavior).

## Testing

Compute (`tests/test_temporal_patterns.py`) — seed PAID orders at chosen local
timestamps:
- revenue bucketed to the correct **weekday** and **hour** (place orders on known
  dates/times; assert the right `DowStat`/`HourStat`).
- **avg-per-occurrence**: a weekday appearing twice in the window with revenue R total
  → `avg_revenue == R/2`; `is_peak` on the right weekday.
- **dayparts** sum the right hours; peak daypart correct.
- **trend shape**: a rising series → "Trending up"; a flat series → "Steady"; a
  high-variance series → "Spiky"; a <8-day window → `has_enough False`, "Not enough data".
- **insights**: strongest weekday + its pct vs daily average; peak hour; best single day.
- **PAID-only** (a SAMPLE order excluded); **empty window** → no crash, all "—"/zero.

Route/render (`tests/test_sales_timing_tab.py`):
- `?tab=timing` returns 200 and renders the callouts + the three panels; the Timing tab
  is now a real link (not the disabled chip).
- Overview (default) + SKUs tab unaffected; period scopes drive the window.

## Out of scope (later / deliberately)

- Phase 3: per-SKU × time heatmap (this tab is aggregate-only).
- A metric toggle (revenue only).
- A Timing CSV export.
- Timezone configurability (uses the existing shop reporting zone).

## Success criteria

1. The Timing tab shows revenue by **day-of-week** (avg per occurrence), by **hour**
   (24 bars), and a **daily trend** with a computed shape label, over the selected period.
2. Insight callouts surface the strongest weekday (+% vs average), peak hour, trend
   shape, and best single day.
3. Edge cases (empty / short / single-day windows) degrade gracefully; Overview + SKUs
   tabs and the full suite stay green.
