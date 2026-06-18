"""ad_budgets + ad_budget_promotions tables

Ad budget tracking: a Smashbox-allocated ad budget (flexible date range +
amount) and manual dated promotion carve-outs. Actual spend is NOT stored —
it auto-pulls from GmvMaxDailyMetric over the budget's range.

Revision ID: a1b8c2d3e4f5
Revises: f7b8c9d0e1f2
Create Date: 2026-06-18

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b8c2d3e4f5"
down_revision: Union[str, Sequence[str], None] = "f7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ad_budgets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("shop_id", sa.Integer(), nullable=True),
        sa.Column("label", sa.String(length=64), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["shop_id"], ["shops.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ad_budgets_shop_id"), "ad_budgets", ["shop_id"])

    op.create_table(
        "ad_budget_promotions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ad_budget_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("promo_date", sa.Date(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["ad_budget_id"], ["ad_budgets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_ad_budget_promotions_ad_budget_id"),
        "ad_budget_promotions",
        ["ad_budget_id"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_ad_budget_promotions_ad_budget_id"), table_name="ad_budget_promotions")
    op.drop_table("ad_budget_promotions")
    op.drop_index(op.f("ix_ad_budgets_shop_id"), table_name="ad_budgets")
    op.drop_table("ad_budgets")
