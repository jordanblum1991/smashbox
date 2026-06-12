"""shop inventory sync schedule columns

Adds the user-editable SAP inventory auto-sync schedule to `shops`:
enabled flag + hour/minute (in the shop's timezone) + day_of_week string.
server_default backfills the single existing shop to weekday 07:30.

Revision ID: f1a2b3c4d5e6
Revises: 07c4ee33b7fa
Create Date: 2026-06-12

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "07c4ee33b7fa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("shops", schema=None) as batch_op:
        # sa.true() renders the dialect-correct boolean literal (true / 1),
        # avoiding the integer-vs-BOOLEAN type error a literal "1" causes on PG.
        batch_op.add_column(sa.Column(
            "inventory_sync_enabled", sa.Boolean(),
            nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column(
            "inventory_sync_hour", sa.Integer(),
            nullable=False, server_default="7"))
        batch_op.add_column(sa.Column(
            "inventory_sync_minute", sa.Integer(),
            nullable=False, server_default="30"))
        batch_op.add_column(sa.Column(
            "inventory_sync_days", sa.String(length=64),
            nullable=False, server_default="mon,tue,wed,thu,fri"))


def downgrade() -> None:
    with op.batch_alter_table("shops", schema=None) as batch_op:
        batch_op.drop_column("inventory_sync_days")
        batch_op.drop_column("inventory_sync_minute")
        batch_op.drop_column("inventory_sync_hour")
        batch_op.drop_column("inventory_sync_enabled")
