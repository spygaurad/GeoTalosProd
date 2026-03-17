"""add processed_items and total_items to jobs

Revision ID: fe91cf034329
Revises: 1b2c3d4e5f60
Create Date: 2026-03-17 00:03:02.540899

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fe91cf034329'
down_revision: Union[str, None] = '1b2c3d4e5f60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add columns to jobs table
    op.add_column('jobs', sa.Column('processed_items', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('jobs', sa.Column('total_items', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    # Remove columns from jobs table
    op.drop_column('jobs', 'processed_items')
    op.drop_column('jobs', 'total_items')