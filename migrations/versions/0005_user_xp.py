"""user xp and levels

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('users', sa.Column('xp', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('users', sa.Column('xp_day', sqlmodel.sql.sqltypes.AutoString(length=8), nullable=True))
    op.add_column('users', sa.Column('xp_today', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('users', sa.Column('last_daily', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'last_daily')
    op.drop_column('users', 'xp_today')
    op.drop_column('users', 'xp_day')
    op.drop_column('users', 'xp')
