"""sample_inventory_snapshots table

Separate on-hand snapshot store for the sample pool (SBS warehouse from the SAP
feed), kept distinct from the sellable `inventory_snapshots`.

Revision ID: b2c3d4e5f6a7
Revises: f1a2b3c4d5e6
Create Date: 2026-06-12

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sample_inventory_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("shop_id", sa.Integer(), nullable=True),
        sa.Column("import_batch_id", sa.Integer(), nullable=False),
        sa.Column("sku", sa.String(length=128), nullable=False),
        sa.Column("on_hand", sa.Integer(), nullable=True),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["import_batch_id"], ["import_batches.id"]),
        sa.ForeignKeyConstraint(["shop_id"], ["shops.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sku", "captured_at", name="uq_sample_inventory_sku_captured_at"),
    )
    with op.batch_alter_table("sample_inventory_snapshots", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_sample_inventory_snapshots_captured_at"), ["captured_at"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_sample_inventory_snapshots_import_batch_id"), ["import_batch_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_sample_inventory_snapshots_shop_id"), ["shop_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_sample_inventory_snapshots_sku"), ["sku"], unique=False)


def downgrade() -> None:
    op.drop_table("sample_inventory_snapshots")
