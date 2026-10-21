"""add job description

Revision ID: b1e18f4fbb2e
Revises: 0001
Create Date: 2026-10-21 16:26:41.812062

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1e18f4fbb2e'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("description", sa.Text(), nullable=True),
        schema="public",
    )


def downgrade() -> None:
    op.drop_column(
        "jobs",
        "description",
        schema="public",
    )
