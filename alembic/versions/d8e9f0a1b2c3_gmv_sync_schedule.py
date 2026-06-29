"""shop GMV-Max (Marketing API) sync schedule columns

Adds the user-editable GMV-Max auto-sync schedule to `shops`: enabled flag +
hour/minute (in the shop's timezone) + day_of_week string. Decouples the ad-data
pull from the inventory job. server_default backfills the existing shop to daily
07:45.

Revision ID: d8e9f0a1b2c3
Revises: a9b8c7d6e5f4
Create Date: 2026-06-29

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d8e9f0a1b2c3"
down_revision: Union[str, Sequence[str], None] = "a9b8c7d6e5f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("shops", schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            "gmv_sync_enabled", sa.Boolean(),
            nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column(
            "gmv_sync_hour", sa.Integer(),
            nullable=False, server_default="7"))
        batch_op.add_column(sa.Column(
            "gmv_sync_minute", sa.Integer(),
            nullable=False, server_default="45"))
        batch_op.add_column(sa.Column(
            "gmv_sync_days", sa.String(length=64),
            nullable=False, server_default="mon,tue,wed,thu,fri,sat,sun"))


def downgrade() -> None:
    with op.batch_alter_table("shops", schema=None) as batch_op:
        batch_op.drop_column("gmv_sync_days")
        batch_op.drop_column("gmv_sync_minute")
        batch_op.drop_column("gmv_sync_hour")
        batch_op.drop_column("gmv_sync_enabled")
