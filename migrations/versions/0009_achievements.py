"""achievements, and best streak + daily challenge per player day

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = '0009'
down_revision: str | None = '0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('player_days', sa.Column('best_streak', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('player_days', sa.Column('daily', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table(
        'user_achievements',
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('achievement', sqlmodel.sql.sqltypes.AutoString(length=24), nullable=False),
        sa.Column('unlocked_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('user_id', 'achievement'),
    )


def downgrade() -> None:
    op.drop_table('user_achievements')
    op.drop_column('player_days', 'daily')
    op.drop_column('player_days', 'best_streak')
