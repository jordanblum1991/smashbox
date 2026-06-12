"""tiktok_sync_state table

Per-stream watermark + last-run status for the TikTok API sync.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-12

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tiktok_sync_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("stream", sa.String(length=32), nullable=False),
        sa.Column("synced_through", sa.DateTime(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_status", sa.String(length=16), nullable=False),
        sa.Column("last_message", sa.Text(), nullable=True),
        sa.Column("rows_last_run", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("tiktok_sync_state", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_tiktok_sync_state_stream"), ["stream"], unique=True)


def downgrade() -> None:
    op.drop_table("tiktok_sync_state")
