"""purchase_orders + purchase_order_lines

Editable purchase orders (draft → placed) seeded from the demand planner.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-12

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "purchase_orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("shop_id", sa.Integer(), nullable=True),
        sa.Column("number", sa.String(length=32), nullable=False),
        sa.Column("supplier", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("placed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["shop_id"], ["shops.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("purchase_orders", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_purchase_orders_number"), ["number"], unique=True)
        batch_op.create_index(batch_op.f("ix_purchase_orders_shop_id"), ["shop_id"], unique=False)

    op.create_table(
        "purchase_order_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("purchase_order_id", sa.Integer(), nullable=False),
        sa.Column("sku", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_cost", sa.Numeric(precision=12, scale=4), nullable=False),
        sa.ForeignKeyConstraint(["purchase_order_id"], ["purchase_orders.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("purchase_order_lines", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_purchase_order_lines_purchase_order_id"),
            ["purchase_order_id"], unique=False)


def downgrade() -> None:
    op.drop_table("purchase_order_lines")
    op.drop_table("purchase_orders")
