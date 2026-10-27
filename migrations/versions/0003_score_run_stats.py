"""score run stats

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-27 10:42:44.127184
"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op


revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('scores', sa.Column('shots', sa.Integer(), nullable=True))
    op.add_column('scores', sa.Column('makes', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('scores', 'makes')
    op.drop_column('scores', 'shots')
