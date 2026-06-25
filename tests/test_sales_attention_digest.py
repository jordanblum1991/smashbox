"""The 'Needs attention' digest for the scheduled sales email — buckets SKUs
into decelerating / spiking / stalled / low-cover, capped per category."""
from decimal import Decimal

from app.reports.dashboard_trends import Delta
from app.reports.sku_performance import SkuPerfRow, build_attention_digest


def _row(sku_id, *, status="steady", pct=None, cover=None, units=10):
    momentum = None
    if pct is not None:
        state = "up" if pct > 0 else "down"
        momentum = Delta(state=state, pct=Decimal(str(pct)), label=f"{pct:+}%")
    return SkuPerfRow(
        sku_id=sku_id, code=f"SBX-{sku_id}", name=sku_id, units=units,
        net_sales=Decimal("0"), orders=units, pct_units=Decimal("0"),
        prior_units=0, momentum=momentum, status=status, spark="",
        days_of_cover=(Decimal(str(cover)) if cover is not None else None),
    )


def test_digest_buckets_by_category():
    rows = [
        _row("DECEL", status="declining", pct=-40, units=50),
        _row("SPIKE", status="rising", pct=80, units=30),
        _row("STALL", status="stalled", pct=None, units=0),
        _row("LOW", status="steady", cover=5, units=20),
        _row("FINE", status="steady", pct=5, cover=200, units=100),  # excluded
    ]
    d = build_attention_digest(rows)
    assert [r.sku_id for r in d.decelerating] == ["DECEL"]
    assert [r.sku_id for r in d.spiking] == ["SPIKE"]
    assert [r.sku_id for r in d.stalled] == ["STALL"]
    assert [r.sku_id for r in d.low_cover] == ["LOW"]
    assert d.any is True
    # FINE appears in no bucket.
    for bucket in (d.decelerating, d.spiking, d.stalled, d.low_cover):
        assert all(r.sku_id != "FINE" for r in bucket)


def test_digest_caps_each_category_and_reports_total():
    rows = [_row(f"D{i}", status="declining", pct=-30, units=i) for i in range(8)]
    d = build_attention_digest(rows, cap=5)
    assert len(d.decelerating) == 5            # capped
    assert d.counts["decelerating"] == 8       # full count for "+N more"
    # Highest-units first within the category.
    assert d.decelerating[0].units == 7


def test_digest_empty_when_all_healthy():
    rows = [_row("FINE", status="steady", pct=2, cover=100)]
    d = build_attention_digest(rows)
    assert d.any is False
