"""tiktok_credentials table

Stores the TikTok Shop API authorization (access/refresh tokens + shop cipher),
captured by the authorize callback and rotated by the refresh job.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-12

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tiktok_credentials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("shop_id", sa.String(length=128), nullable=True),
        sa.Column("shop_cipher", sa.String(length=255), nullable=True),
        sa.Column("shop_name", sa.String(length=255), nullable=True),
        sa.Column("seller_name", sa.String(length=255), nullable=True),
        sa.Column("region", sa.String(length=16), nullable=True),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=False),
        sa.Column("access_expires_at", sa.DateTime(), nullable=True),
        sa.Column("refresh_expires_at", sa.DateTime(), nullable=True),
        sa.Column("granted_scopes", sa.Text(), nullable=True),
        sa.Column("connected_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("tiktok_credentials")
