"""Demand-planner key-space consolidation.

Regression guard for the bug where one physical SKU fragmented into multiple
planner rows because on-hand and velocity live in different key-spaces:

  - on-hand (SAP feed -> InventorySnapshot.sku) is keyed by the SBX-form
    physical code (e.g. "SBX-C6NF01")
  - velocity (OrderLine.sku, rewritten by the resolver) is keyed by the
    canonical TikTok SKU ID (numeric), and the same product may also have
    stray order lines under its C-form alt SKU

Before the fix the planner did `set(velocities) | set(on_hand_by_sku)` and
matched on-hand by exact key, so the velocity row read on_hand=0 -> OUT_OF_STOCK
while the real units sat on a separate row. The planner must consolidate every
identifier of one physical product onto a single row carrying the real on-hand.
"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models.import_batch import ImportBatch, ImportBatchStatus, ImportFileKind
from app.models.inventory_snapshot import InventorySnapshot
from app.models.order import Order, OrderLine, OrderType
from app.models.sku import Sku
from app.reports.demand_planning import (
    compute_demand_planning_view,
    compute_sku_detail_view,
)
from app.services.demand.replenishment import ReplenishmentStatus

AS_OF = datetime(2026, 6, 23, 12, 0, 0)


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _add_sale(db, batch, *, sku: str, qty: int, when: datetime, n: int):
    """One PAID, Shipped order line for `sku` so it registers as velocity."""
    order = Order(
        import_batch_id=batch.id,
        tiktok_order_id=f"ORD-{sku}-{n}",
        placed_at=when,
        order_type=OrderType.PAID,
        status="Shipped",
        brand="Smashbox",
    )
    order.lines.append(OrderLine(sku=sku, quantity=qty))
    db.add(order)


def _seed_c6nf01(db):
    """One physical SKU: SAP on-hand under the SBX-form code, velocity under the
    TikTok ID. Returns nothing; commits the scenario."""
    tiktok_id = "1729488823947269291"
    batch = ImportBatch(
        kind=ImportFileKind.INVENTORY_SNAPSHOT,
        status=ImportBatchStatus.COMPLETED,
        original_filename="seed", stored_path="",
    )
    db.add(batch)
    db.flush()
    db.add(Sku(
        sku="SBX-C6NF01",
        tiktok_sku_id=tiktok_id,
        tiktok_alt_sku="C6NF01",
        name="Be Legendary Line & Prime Pencil",
        brand="Smashbox",
        unit_cogs="12.00",
        is_reorderable=True,
    ))
    db.add(InventorySnapshot(
        import_batch_id=batch.id, sku="SBX-C6NF01", on_hand=4,
        captured_at=datetime(2026, 6, 23, 0, 0, 0),
    ))
    _add_sale(db, batch, sku=tiktok_id, qty=1, when=datetime(2026, 6, 5), n=1)
    _add_sale(db, batch, sku=tiktok_id, qty=1, when=datetime(2026, 6, 18), n=2)
    db.commit()
    return tiktok_id


def test_onhand_and_velocity_consolidate_to_one_physical_row():
    """A SKU with SAP on-hand keyed by its SBX-form code and sales velocity
    keyed by its TikTok SKU ID must produce ONE planner row that carries the
    real on-hand (4) — never a phantom OUT_OF_STOCK row at on_hand=0."""
    with SessionLocal() as db:
        _seed_c6nf01(db)
        view = compute_demand_planning_view(db, as_of=AS_OF)

    rows = [r for r in view.rows if r.sku_code == "SBX-C6NF01"]
    assert len(rows) == 1, (
        f"expected one consolidated row, got {len(rows)}: "
        + ", ".join(f"{r.component_sku}/{r.status.value}/oh={r.on_hand}" for r in rows)
    )
    assert rows[0].on_hand == 4
    assert rows[0].status != ReplenishmentStatus.OUT_OF_STOCK


def test_drilldown_uses_physical_code_from_table():
    """The table now links drill-downs by the physical code (Sku.sku). The
    drill-down must resolve that code: show the real on-hand AND the folded
    velocity — not a 404 or an empty/no-velocity view."""
    with SessionLocal() as db:
        _seed_c6nf01(db)
        # The component_sku the table row now carries is the physical code.
        detail = compute_sku_detail_view(db, "SBX-C6NF01", as_of=AS_OF)

    assert detail is not None, "drill-down 404'd on the physical code the table emits"
    assert detail.row.on_hand == 4
    assert detail.row.daily_velocity > 0
    assert detail.row.status != ReplenishmentStatus.OUT_OF_STOCK
    # Weekly velocity chart must reflect the 2 units sold (folded from the
    # TikTok-ID order lines), not an empty series.
    assert sum(b.units for b in detail.weekly_velocity) == 2


def test_procurement_save_handles_multivariation_physical_code():
    """A physical SKU with multiple TikTok variations shares one Sku.sku. The
    procurement POST (now keyed by the physical code) must not 500 on the
    multi-row match, and the edit must land on every variation so the planner's
    representative row reflects it."""
    with SessionLocal() as db:
        batch = ImportBatch(
            kind=ImportFileKind.SKU_MASTER,
            status=ImportBatchStatus.COMPLETED,
            original_filename="seed", stored_path="",
        )
        db.add(batch)
        db.flush()
        # Two variations of one physical product, same SBX-form code.
        db.add(Sku(sku="SBX-C2NP19", tiktok_sku_id="1732267408199094443",
                   tiktok_alt_sku="C2NP19", name="Always On Liquid Lipstick",
                   brand="Smashbox", unit_cogs="13.50", is_reorderable=True))
        db.add(Sku(sku="SBX-C2NP19", tiktok_sku_id="1729488780968563883",
                   tiktok_alt_sku="C2NP19", name="Always On Liquid Lipstick",
                   brand="Smashbox", unit_cogs="13.50", is_reorderable=True))
        db.commit()

    client = TestClient(app)
    resp = client.post(
        "/reports/demand-planning/sku/SBX-C2NP19/procurement",
        data={"lead_time_days": "30", "is_reorderable": "on"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text[:300]

    with SessionLocal() as db:
        rows = db.query(Sku).filter(Sku.sku == "SBX-C2NP19").all()
        assert len(rows) == 2
        assert all(r.lead_time_days == 30 for r in rows)
