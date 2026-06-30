"""sample_inbound_orders + sample_inbound_order_lines

Incoming (on-order) sample stock — counts as inbound sample inventory while OPEN,
clears on RECEIVED. $0 cost (no cost column).

Revision ID: a1b2c3d4e5f7
Revises: d8e9f0a1b2c3
Create Date: 2026-06-30

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f7"
down_revision: Union[str, Sequence[str], None] = "d8e9f0a1b2c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sample_inbound_orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("shop_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["shop_id"], ["shops.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("sample_inbound_orders", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_sample_inbound_orders_shop_id"), ["shop_id"], unique=False)

    op.create_table(
        "sample_inbound_order_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sample_inbound_order_id", sa.Integer(), nullable=False),
        sa.Column("sku", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["sample_inbound_order_id"], ["sample_inbound_orders.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("sample_inbound_order_lines", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_sample_inbound_order_lines_sample_inbound_order_id"),
            ["sample_inbound_order_id"], unique=False)


def downgrade() -> None:
    op.drop_table("sample_inbound_order_lines")
    op.drop_table("sample_inbound_orders")
