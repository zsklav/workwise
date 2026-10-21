"""Create the initial jobs table.

Revision ID: 0001
Revises: None
"""

from alembic import op
import sqlalchemy as sa


revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), sa.Identity(always=True), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("company", sa.Text(), nullable=False),
        sa.Column("location", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="jobs_pkey"),
        schema="public",
    )


def downgrade() -> None:
    op.drop_table("jobs", schema="public")
