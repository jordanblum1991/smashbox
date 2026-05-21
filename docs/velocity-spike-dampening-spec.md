# Spec: Spike-Dampened Velocity for Smashbox Demand Planning

**Target file:** `app/services/demand/velocity.py` (function `compute_velocity`)
**Related:** `app/services/demand/replenishment.py`, `app/config.py`
**Status:** Ready to implement. Validated against `All_order-2026-05-13-10_38.csv` (60-day window ending 2026-05-12, 77 active SKUs).

---

## Problem

`daily_60d` drives all order math (reorder point, suggested qty, days of supply — see logic doc §2.8.4). It is a flat mean over the 60-day window with **no outlier handling** (§2.8.3, §2.8.5). Smashbox demand is creator-driven and spikes are **organic, not planned** — ad spend is a fixed monthly floor, so spikes can't be predicted from a campaign calendar. A single viral day enters the 60-day window and inflates the baseline for the full 60 days, causing systematic **overbuying in the ~6–8 weeks after a spike fades**. The existing `trend_ratio` surfaces this but changes no math; it relies on a buyer noticing and acting.

Manual campaign tagging was rejected: with organic spikes you'd be tagging after the fact, which is a slower, more error-prone version of letting statistics catch it. **Statistical dampening is the primary mechanism; manual exclusion is a rare override.**

## What the data showed (the important part)

The first-draft design was: cap each day at `3 × median(non-zero daily units)`, but **skip capping for any SKU with fewer than 7 selling days** in the window (to dodge the zero-median problem). Testing against real orders revealed this **inverts the intended protection**:

- Only **11 of 77** active SKUs sell on ≥7 distinct days. Those are the hero SKUs.
- The other **66 SKUs** fall below the floor and get **zero spike protection** — they fall back to raw `daily_60d`.
- The long tail is the *most* spike-vulnerable part of the catalog (a SKU selling ~1 unit/week, then 30 in one creator afternoon, dumps all 30 into the baseline).

So the floor protected the 11 SKUs that needed it least and abandoned the 66 that needed it most. This looks fine in code review and quietly overbuys the long tail for months.

## The fix: two-armed cap, total-units gate (no selling-days floor)

For each component SKU, build the 60-day daily-units series (bundle-expanded, zeros filled for non-selling days), then:

```
total_units   = sum of daily series over the 60-day window

# Gate: don't smooth near-dead SKUs (replaces the selling-days floor)
if total_units < MIN_UNITS_FOR_DAMPENING:   # 5
    daily_60d_robust = daily_60d_raw
else:
    median_nz = median of non-zero days        # 0 if no selling days
    raw_mean  = daily_60d_raw                   # mean incl. zeros
    cap_day   = max(SPIKE_CAP_MULT * median_nz,  RAW_MEAN_MULT * raw_mean)
    capped    = clip each day at cap_day
    daily_60d_robust = mean(capped)
```

**Why two arms.** The median arm (`3 × median_nz`) is lenient for steady hero SKUs — their busy days rarely exceed it, so they're untouched. The mean arm (`5 × raw_mean`) provides a sane ceiling for intermittent SKUs where `median_nz` is tiny or degenerate, so the long tail gets protection instead of nothing. The arms cross over right where they should: steady SKU → median arm wins; lumpy SKU → mean arm wins. `max()` of the two means a SKU is only ever capped when it exceeds *both* — i.e. a genuine outlier day, not a normal good day.

**Why a total-units gate, not a selling-days floor.** The gate preserves the original intent ("don't churn on dead SKUs") without the side effect of dropping live-but-lumpy SKUs. Under ~5 units in 60 days there's nothing to smooth and the SKU is likely headed for `NO_VELOCITY` anyway.

## Constants (add to `app/config.py`)

| Name | Value | Purpose |
|------|-------|---------|
| `velocity_spike_cap_mult` | `3.0` | Median-arm multiple. 3× validated as correct for hero SKUs — only the 3 genuinely spiky SKUs trip >20% dampening, which is desired. |
| `velocity_raw_mean_mult` | `5.0` | Mean-arm multiple. Protects intermittent SKUs whose median-arm cap is degenerate. |
| `velocity_min_units_for_dampening` | `5` | Below this total in the window, robust = raw (no smoothing). |

All three tunable in one place; no code change to retune.

## Output contract

Keep **both** numbers on the velocity output object:

- `daily_60d_raw` — existing flat mean. **Do not remove** — still feeds `trend_ratio` and buyer trust.
- `daily_60d_robust` — new dampened rate.

The gap between them *is* the spike indicator (more direct than `trend_ratio` for this purpose).

## Where each number is used (the deliberate asymmetry — CONFIRM ON IMPLEMENT)

- **Buying decisions use `robust`:** reorder_point, target_units, suggested_qty, investment outlook. This is where overbuying happens; be conservative.
- **Stockout warnings use `raw`:** days_of_supply, stockout_date, and the `at_risk` / `out_of_stock` flags. For stockout *risk* you want the more pessimistic (higher, undampened) velocity — better to flag a stockout early than be lulled by a dampened rate if a spike is real and ongoing.

This asymmetry (conservative about buying, aggressive about flagging risk) is the one design choice worth re-examining at implementation. The alternative — one consistent velocity everywhere — is simpler but makes stockout flags less sensitive, which is the wrong direction for those flags.

## Keep the trend-ratio brake too (separate from the cap)

Winsorizing tames a spike *while it's in the window*. ~60 days later, the capped-but-elevated days roll out and the baseline can be stale-high after demand already normalized. `trend_ratio = daily_14d / daily_60d` dropping well below ~0.6 is the early warning that the baseline is about to age out high. Cap handles spike *magnitude*; trend-ratio handles its *aftermath*. Not redundant.

## Validation caveats (don't over-trust the offline numbers)

The offline analysis used raw `Seller SKU` with **no bundle fan-out** and `Created Time` (= `placed_at`) under a `status IN (Shipped, Completed)` filter. Two consequences for the real implementation:

1. **Bundle expansion will raise effective selling-day counts** for component SKUs that appear across multiple bundles. So in production fewer SKUs are as intermittent as the 66/77 figure suggests — the long-tail problem is real but somewhat smaller. The two-armed cap handles this correctly either way (it's not floor-dependent).
2. Offline couldn't replicate `PAID_SAMPLE` inclusion (raw export lacks `order_type`). The real `compute_velocity` filter per §2.1 is `order_type IN (PAID, PAID_SAMPLE)` AND `status IN (Shipped, Completed)`. Implement against the real filter.

Implement the cap *after* bundle expansion, on the component-SKU daily series — same place `daily_60d` is computed today.

## Suggested test

Add a unit test that constructs a synthetic 60-day series: flat baseline + one outlier day at 10× baseline. Assert `daily_60d_robust < daily_60d_raw`, that the outlier day is clipped to `cap_day`, and that a no-spike series yields `robust == raw`. Add a degenerate case (3 selling days, total ≥5) to confirm the mean arm engages and the result isn't zeroed.

