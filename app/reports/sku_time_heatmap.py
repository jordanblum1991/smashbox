# app/reports/sku_time_heatmap.py
"""SKU × time heatmap for the Heatmap tab of /reports/sales: PAID units per SKU
bucketed by shop-local day-of-week or daypart, ranked top-N by total units, with
per-row colour levels (each SKU shaded against its own peak bucket). Pure
computation — reads the ORM, returns dataclasses.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import floor

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.order import Order, OrderLine, OrderType
from app.services.reporting_tz import placed_local, placed_window
from app.services.sku_resolver import catalog_label_map

DIMS = ("dow", "daypart")
HEAT_LEVELS = 5                 # 0 (none) … 4 (per-row peak)

_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_DAYPARTS = ["Morning", "Afternoon", "Evening", "Night"]


def _daypart_bucket(hour: int) -> int:
    """Same partition as temporal_patterns: Morning 5–11, Afternoon 12–16,
    Evening 17–21, Night 22–4."""
    if 5 <= hour < 12:
        return 0
    if 12 <= hour < 17:
        return 1
    if 17 <= hour < 22:
        return 2
    return 3


@dataclass
class HeatCell:
    bucket: int
    label: str
    units: int
    level: int


@dataclass
class HeatRow:
    sku_id: str
    code: str
    name: str
    total_units: int
    cells: list[HeatCell]
    peak_label: str


@dataclass
class HeatmapView:
    columns: list[str]
    rows: list[HeatRow]
    dim: str
    total_skus: int
    shown: int
    busiest_col: str | None
    window_start: date
    window_end: date


def compute_sku_time_heatmap(db: Session, *, start: date, end: date,
                             dim: str = "dow", top_n: int = 20) -> HeatmapView:
    if dim not in DIMS:
        dim = "dow"
    columns = _WEEKDAYS if dim == "dow" else _DAYPARTS
    n_cols = len(columns)

    q_start = datetime(start.year, start.month, start.day)
    q_end = datetime(end.year, end.month, end.day) + timedelta(days=1)
    src_start, src_end = placed_window(q_start, q_end)

    lines = db.execute(
        select(OrderLine.sku, OrderLine.quantity, Order.placed_at)
        .join(Order, Order.id == OrderLine.order_id)
        .where(Order.order_type == OrderType.PAID)
        .where(Order.placed_at >= src_start)
        .where(Order.placed_at < src_end)
    ).all()

    units: dict[str, list[int]] = defaultdict(lambda: [0] * n_cols)
    col_totals = [0] * n_cols
    for sku, qty, placed in lines:
        local = placed_local(placed)
        bucket = local.weekday() if dim == "dow" else _daypart_bucket(local.hour)
        units[sku][bucket] += qty
        col_totals[bucket] += qty

    total_skus = len(units)
    # Single SKUs AND bundles, so a bundle sold via TikTok isn't "Unmapped".
    catalog = catalog_label_map(db)

    def _code(sku: str) -> str:
        return catalog.get(sku, ("Unmapped", ""))[0]

    ranked = sorted(units.items(), key=lambda kv: (-sum(kv[1]), _code(kv[0])))[:top_n]

    rows: list[HeatRow] = []
    for sku, buckets in ranked:
        code, name = catalog.get(sku, ("Unmapped", f"Unmapped SKU {sku}"))
        row_peak = max(buckets)
        cells = []
        for i, u in enumerate(buckets):
            if u == 0 or row_peak == 0:
                level = 0
            else:
                level = 1 + floor((u / row_peak) * (HEAT_LEVELS - 2))
                # Guard: keeps level in [1, HEAT_LEVELS-1] if the formula/levels change.
                level = max(1, min(level, HEAT_LEVELS - 1))
            cells.append(HeatCell(bucket=i, label=columns[i], units=u, level=level))
        peak_i = max(range(n_cols), key=lambda i: buckets[i]) if row_peak > 0 else None
        rows.append(HeatRow(sku_id=sku, code=code, name=name, total_units=sum(buckets),
                            cells=cells, peak_label=(columns[peak_i] if peak_i is not None else "")))

    busiest_col = columns[max(range(n_cols), key=lambda i: col_totals[i])] if any(col_totals) else None

    return HeatmapView(columns=columns, rows=rows, dim=dim, total_skus=total_skus,
                       shown=len(rows), busiest_col=busiest_col,
                       window_start=start, window_end=end)
