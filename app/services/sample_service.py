"""Creator Sample Module — service layer.

Three write functions and one read function. No routers, no reports.

Write contract (same as all other services in this codebase):
  Functions flush but do NOT commit. The caller commits once it is satisfied
  that all writes in the transaction are correct. A failure at any point before
  commit leaves the DB unchanged.

SYNC INVARIANT (record_sample_shipment):
  A Sample shipment row and its corresponding SampleInventoryMovement OUT row
  MUST be written in the same transaction and MUST be committed together.
  This function flushes both before returning; the caller's commit makes them
  durable together. If anything raises between the two flushes, neither row
  is committed. "Samples sent" and "inventory drawn down" must never drift.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.creator import Creator
from app.models.sample import Sample
from app.models.sample_inventory_movement import SampleInventoryMovement, SampleMovementType
from app.services.sku_alias import load_alias_map


def get_or_create_creator(
    db: Session,
    *,
    handle: str,
    brand: str,
    platform: str = "unknown",
    name: str | None = None,
    shop_id: int | None = None,
) -> Creator:
    """Return existing Creator for (shop_id, handle, platform), or create one.

    Routes through the ORM so the platform='unknown' default applies on insert
    and the unique constraint (shop_id, handle, platform) is respected.
    """
    shop_id_filter = (
        Creator.shop_id.is_(None) if shop_id is None else Creator.shop_id == shop_id
    )
    existing = db.execute(
        select(Creator).where(
            Creator.handle == handle,
            Creator.platform == platform,
            shop_id_filter,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    creator = Creator(
        handle=handle,
        platform=platform,
        brand=brand,
        name=name,
        shop_id=shop_id,
    )
    db.add(creator)
    db.flush()
    return creator


def record_sample_shipment(
    db: Session,
    *,
    sku: str,
    quantity: int,
    shipped_at: datetime,
    brand: str,
    import_batch_id: int,
    creator_handle: str | None = None,
    creator_id: int | None = None,
    shipping_cost: Decimal | None = None,
    is_paid_oversample: bool = False,
    note: str | None = None,
    shop_id: int | None = None,
    alias_map: dict[str, str] | None = None,
) -> tuple[Sample, SampleInventoryMovement]:
    """Record a sample sent to a creator.

    SYNC INVARIANT: creates Sample + SampleInventoryMovement OUT in the same
    transaction. Flushes both; caller commits. If anything raises between the
    two flushes, the caller's session rolls back and neither row persists.

    Canonicalizes sku via alias_map before writing. If creator_handle is given
    and creator_id is not, auto-resolves (or creates) the Creator row so the
    FK is populated — existing CSV-imported rows that only carry creator_handle
    still work without a creator_id.
    """
    if alias_map is None:
        alias_map = load_alias_map(db)
    canonical_sku = alias_map.get(sku, sku)

    # Resolve creator FK from handle when not provided explicitly.
    if creator_handle and creator_id is None:
        creator = get_or_create_creator(
            db,
            handle=creator_handle,
            brand=brand,
            shop_id=shop_id,
        )
        creator_id = creator.id

    sample = Sample(
        import_batch_id=import_batch_id,
        shop_id=shop_id,
        shipped_at=shipped_at,
        sku=canonical_sku,
        quantity=quantity,
        creator_handle=creator_handle,
        creator_id=creator_id,
        shipping_cost=shipping_cost,
        is_paid_oversample=is_paid_oversample,
        note=note,
    )
    db.add(sample)
    db.flush()  # materialize sample.id for the ledger FK

    movement = SampleInventoryMovement(
        shop_id=shop_id,
        brand=brand,
        sku=canonical_sku,
        movement_type=SampleMovementType.OUT,
        quantity=quantity,
        moved_at=shipped_at,
        sample_id=sample.id,
        note=note,
    )
    db.add(movement)
    db.flush()

    return sample, movement


def record_sample_receipt(
    db: Session,
    *,
    sku: str,
    quantity: int,
    received_at: datetime,
    brand: str,
    unit_cost: Decimal | None = None,
    import_batch_id: int | None = None,
    note: str | None = None,
    shop_id: int | None = None,
    alias_map: dict[str, str] | None = None,
) -> SampleInventoryMovement:
    """Record units received from supplier into the sample pool.

    Creates a single SampleInventoryMovement IN row. Canonicalizes sku.
    unit_cost is dormant (null = $0 for now); populate when supplier invoices
    are available — no migration required since the column is already nullable.
    """
    if alias_map is None:
        alias_map = load_alias_map(db)
    canonical_sku = alias_map.get(sku, sku)

    movement = SampleInventoryMovement(
        shop_id=shop_id,
        import_batch_id=import_batch_id,
        brand=brand,
        sku=canonical_sku,
        movement_type=SampleMovementType.IN,
        quantity=quantity,
        moved_at=received_at,
        unit_cost=unit_cost,
        note=note,
    )
    db.add(movement)
    db.flush()
    return movement


def get_sample_on_hand(
    db: Session,
    *,
    brand: str | None = None,
    shop_id: int | None = None,
    alias_map: dict[str, str] | None = None,
) -> dict[str, int]:
    """Derive current sample inventory balance per canonical SKU from the ledger.

    Balance per SKU = SUM(IN quantities) − SUM(OUT quantities).
    Returns {canonical_sku: on_hand_units}. SKUs at zero are omitted.

    Applies alias_map defensively at read time — covers ledger rows written
    before an alias was registered (the writers canonicalize on insert, but
    a receipt written before an alias existed will carry the legacy code).
    Brand and shop_id filter when provided.
    """
    if alias_map is None:
        alias_map = load_alias_map(db)

    q = (
        select(
            SampleInventoryMovement.sku,
            SampleInventoryMovement.movement_type,
            func.coalesce(func.sum(SampleInventoryMovement.quantity), 0).label("total"),
        )
        .group_by(SampleInventoryMovement.sku, SampleInventoryMovement.movement_type)
    )
    if brand is not None:
        q = q.where(SampleInventoryMovement.brand == brand)
    if shop_id is not None:
        q = q.where(SampleInventoryMovement.shop_id == shop_id)

    raw: dict[str, int] = {}
    for sku, movement_type, total in db.execute(q).all():
        canonical = alias_map.get(sku, sku)
        sign = 1 if movement_type == SampleMovementType.IN else -1
        raw[canonical] = raw.get(canonical, 0) + sign * int(total)

    return {sku: bal for sku, bal in raw.items() if bal != 0}
