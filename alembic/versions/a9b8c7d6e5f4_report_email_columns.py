"""report email columns on shops

Revision ID: a9b8c7d6e5f4
Revises: c2d3e4f5a6b7
"""
import sqlalchemy as sa
from alembic import op

revision = "a9b8c7d6e5f4"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None

_COLS = [
    ("sales_report_enabled", sa.Boolean(), sa.false()),
    ("sales_report_hour", sa.Integer(), sa.text("8")),
    ("sales_report_minute", sa.Integer(), sa.text("0")),
    ("sales_report_days", sa.String(length=64), sa.text("'mon'")),
    ("sales_report_recipients", sa.String(length=1024), sa.text("''")),
    ("sales_report_period", sa.String(length=32), sa.text("'prev_month'")),
    ("sample_report_enabled", sa.Boolean(), sa.false()),
    ("sample_report_hour", sa.Integer(), sa.text("8")),
    ("sample_report_minute", sa.Integer(), sa.text("0")),
    ("sample_report_days", sa.String(length=64), sa.text("'mon'")),
    ("sample_report_recipients", sa.String(length=1024), sa.text("''")),
    ("sample_report_period", sa.String(length=32), sa.text("'prev_month'")),
]


def upgrade() -> None:
    for name, type_, default in _COLS:
        op.add_column("shops", sa.Column(name, type_, nullable=False, server_default=default))
    if op.get_bind().dialect.name != "sqlite":
        for name, _type, _default in _COLS:
            op.alter_column("shops", name, server_default=None)


def downgrade() -> None:
    for name, _type, _default in reversed(_COLS):
        op.drop_column("shops", name)
