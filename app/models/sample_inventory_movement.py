"""Sample inventory ledger — auditable record of every sample stock movement.

The current on-hand sample balance per SKU is DERIVED by summing this ledger:
    on_hand = SUM(quantity WHERE movement_type='in')
            - SUM(quantity WHERE movement_type='out')

No stored balance column. The ledger IS the balance, same principle as
double-entry bookkeeping — history is never overwritten, corrections are new rows.

Two movement kinds:
    IN  — units received from supplier into the sample pool.
    OUT — units shipped to a creator, decrementing the sample pool.

Sample inventory is a wholly separate pool from sellable on-hand
(InventorySnapshot). Sample movements never touch Order, OrderLine, or
InventorySnapshot, and never enter the velocity / reorder calc.
"""
import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.import_batch import _utc_now_naive


class SampleMovementType(str, enum.Enum):
    IN  = "in"   # units received from supplier into sample pool
    OUT = "out"  # units shipped to a creator, decrementing sample pool


class SampleInventoryMovement(Base):
    __tablename__ = "sample_inventory_movements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    shop_id: Mapped[int | None] = mapped_column(
        ForeignKey("shops.id"), index=True, nullable=True
    )
    # Multi-tenancy Phase 2a.

    import_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_batches.id"), nullable=True, index=True
    )
    # Dormant until a supplier-receipt CSV importer exists. Nullable so
    # manually-entered rows need no batch. Included now so a future importer
    # gets the same batch-rollback capability Sample already has — cheaper
    # than retrofitting onto a populated ledger.

    brand: Mapped[str] = mapped_column(String(64), index=True)
    # Same field as Order.brand / Sku.brand — multi-brand filter hook.

    sku: Mapped[str] = mapped_column(String(128), index=True)
    # Canonical SKU string. Callers MUST apply load_alias_map() before writing
    # so a re-coded SKU's history isn't split. String, not a FK — same contract
    # as Sample.sku and OrderLine.sku, so legacy codes without a Sku row work.

    movement_type: Mapped[SampleMovementType] = mapped_column(
        Enum(SampleMovementType, name="samplemovementtype")
    )
    # IN = supplier receipt; OUT = creator shipment.
    # Balance = SUM(qty WHERE IN) − SUM(qty WHERE OUT).

    quantity: Mapped[int] = mapped_column(Integer)
    # Always a positive integer. Sign is determined by movement_type.
    # Corrections are new rows — never negative quantities.

    moved_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    # When the physical movement occurred — supplier delivery date for IN,
    # ship date for OUT. Separate from created_at so backdated entries are
    # possible without losing the audit trail.

    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    # DORMANT TODAY. Populated only on IN rows when supplier cost is known.
    # Null = $0 for now; no COGS surfaced anywhere while null.
    # Nullable so cost can be recorded later with no migration required.
    # Always leave null on OUT rows.

    sample_id: Mapped[int | None] = mapped_column(
        ForeignKey("samples.id"), nullable=True, index=True
    )
    # Optional traceability FK back to the Sample shipment row that triggered
    # this OUT. Null for IN receipts and any OUT not tied to a single Sample row.
    #
    # SYNC INVARIANT (enforced by service layer, not schema):
    # When a Sample shipment is recorded, the corresponding ledger OUT MUST be
    # written in the same transaction. "Samples sent" and "inventory drawn down"
    # must never drift. Schema allows divergence; the write path must prevent it.

    note: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Freeform audit note — PO number, creator name, adjustment reason, etc.

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive)
    # When this ledger row was written — immutable audit field.
    # Distinct from moved_at so backdated entries remain auditable.
