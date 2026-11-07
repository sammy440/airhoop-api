"""personal bests and ghosts, kept with the account

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = '0008'
down_revision: str | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'personal_bests',
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('key', sqlmodel.sql.sqltypes.AutoString(length=24), nullable=False),
        sa.Column('points', sa.Integer(), nullable=False),
        sa.Column('makes', sa.Integer(), nullable=False),
        sa.Column('streak', sa.Integer(), nullable=False),
        sa.Column('at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.Column('ghost', sa.JSON(), nullable=True),
        sa.Column('updated_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('user_id', 'key'),
    )


def downgrade() -> None:
    op.drop_table('personal_bests')
