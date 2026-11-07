"""skill rating

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0010'
down_revision: str | None = '0009'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('users', sa.Column('rating', sa.Integer(), nullable=False, server_default='1000'))
    op.add_column('users', sa.Column('rated_games', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('match_players', sa.Column('rating_change', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('match_players', 'rating_change')
    op.drop_column('users', 'rated_games')
    op.drop_column('users', 'rating')
