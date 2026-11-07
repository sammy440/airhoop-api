"""match history and shooting trends: match_players, player_days, shot detail on throws

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = '0007'
down_revision: str | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

THROW_COLUMNS = ('aim', 'swish', 'bank', 'miss', 'depth_error', 'side_error', 'green')


def upgrade() -> None:
    op.add_column('matches', sa.Column('race_seconds', sa.Integer(), nullable=True))
    op.add_column('matches', sa.Column('reason', sqlmodel.sql.sqltypes.AutoString(length=16), nullable=True))

    op.add_column('throws', sa.Column('aim', sa.Float(), nullable=True))
    op.add_column('throws', sa.Column('swish', sa.Boolean(), nullable=True))
    op.add_column('throws', sa.Column('bank', sa.Boolean(), nullable=True))
    op.add_column('throws', sa.Column('miss', sqlmodel.sql.sqltypes.AutoString(length=8), nullable=True))
    op.add_column('throws', sa.Column('depth_error', sa.Float(), nullable=True))
    op.add_column('throws', sa.Column('side_error', sa.Float(), nullable=True))
    op.add_column('throws', sa.Column('green', sa.Boolean(), nullable=True))

    op.create_table(
        'match_players',
        sa.Column('match_id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('rank', sa.Integer(), nullable=False),
        sa.Column('won', sa.Boolean(), nullable=False),
        sa.Column('forfeited', sa.Boolean(), nullable=False),
        sa.Column('points', sa.Integer(), nullable=False),
        sa.Column('shots', sa.Integer(), nullable=False),
        sa.Column('makes', sa.Integer(), nullable=False),
        sa.Column('swishes', sa.Integer(), nullable=False),
        sa.Column('banks', sa.Integer(), nullable=False),
        sa.Column('best_streak', sa.Integer(), nullable=False),
        sa.Column('letters', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['match_id'], ['matches.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('match_id', 'user_id'),
    )
    op.create_index(op.f('ix_match_players_user_id'), 'match_players', ['user_id'], unique=False)

    op.create_table(
        'player_days',
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('day', sqlmodel.sql.sqltypes.AutoString(length=8), nullable=False),
        sa.Column('source', sqlmodel.sql.sqltypes.AutoString(length=8), nullable=False),
        sa.Column('shots', sa.Integer(), nullable=False),
        sa.Column('makes', sa.Integer(), nullable=False),
        sa.Column('swishes', sa.Integer(), nullable=False),
        sa.Column('banks', sa.Integer(), nullable=False),
        sa.Column('misses', sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('user_id', 'day', 'source'),
    )


def downgrade() -> None:
    op.drop_table('player_days')
    op.drop_index(op.f('ix_match_players_user_id'), table_name='match_players')
    op.drop_table('match_players')
    for c in THROW_COLUMNS:
        op.drop_column('throws', c)
    op.drop_column('matches', 'reason')
    op.drop_column('matches', 'race_seconds')
