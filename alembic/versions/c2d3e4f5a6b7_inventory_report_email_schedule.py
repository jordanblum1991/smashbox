"""inventory report email schedule columns

Adds the admin-managed weekly inventory-report email config to `shops`:
enabled flag + hour/minute (shop timezone) + day_of_week string + recipients.
Defaults: disabled, Monday 08:00, no recipients.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-06-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("shops", schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            "inventory_report_enabled", sa.Boolean(),
            nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column(
            "inventory_report_hour", sa.Integer(),
            nullable=False, server_default="8"))
        batch_op.add_column(sa.Column(
            "inventory_report_minute", sa.Integer(),
            nullable=False, server_default="0"))
        batch_op.add_column(sa.Column(
            "inventory_report_days", sa.String(length=64),
            nullable=False, server_default="mon"))
        batch_op.add_column(sa.Column(
            "inventory_report_recipients", sa.String(length=1024),
            nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("shops", schema=None) as batch_op:
        batch_op.drop_column("inventory_report_recipients")
        batch_op.drop_column("inventory_report_days")
        batch_op.drop_column("inventory_report_minute")
        batch_op.drop_column("inventory_report_hour")
        batch_op.drop_column("inventory_report_enabled")
