"""skus.family — manual family-grouping override for the inventory report

Revision ID: b2c3d4e5f6a8
Revises: a1b2c3d4e5f7
Create Date: 2026-06-30

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a8"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("skus", schema=None) as batch_op:
        batch_op.add_column(sa.Column("family", sa.String(length=128), nullable=True))
        batch_op.create_index(batch_op.f("ix_skus_family"), ["family"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("skus", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_skus_family"))
        batch_op.drop_column("family")
