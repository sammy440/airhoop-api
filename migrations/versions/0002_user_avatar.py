"""user avatar

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-26 22:05:33.843549
"""
from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op


revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('users', sa.Column('avatar', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'avatar')
