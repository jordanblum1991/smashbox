"""tiktok_marketing_credentials table

Stores the TikTok Marketing API authorization (single long-lived advertiser
access token), captured by the /auth/tiktok-ads/callback flow. Separate from
tiktok_credentials (the Shop API).

Revision ID: f7b8c9d0e1f2
Revises: e5f6a7b8c9d0
Create Date: 2026-06-16

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f7b8c9d0e1f2"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tiktok_marketing_credentials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("advertiser_id", sa.String(length=64), nullable=True),
        sa.Column("advertiser_ids", sa.Text(), nullable=True),
        sa.Column("advertiser_name", sa.String(length=255), nullable=True),
        sa.Column("granted_scopes", sa.Text(), nullable=True),
        sa.Column("connected_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("tiktok_marketing_credentials")
