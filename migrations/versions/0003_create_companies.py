"""Create companies with unique names.

Revision ID: 0003
Revises: b1e18f4fbb2e
"""

from alembic import op
import sqlalchemy as sa


revision = "0003"
down_revision = "b1e18f4fbb2e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("id", sa.Integer(), sa.Identity(always=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="companies_pkey"),
        sa.UniqueConstraint("name", name="uq_companies_name"),
        sa.CheckConstraint(
            "length(btrim(name)) > 0", name="ck_companies_name_not_blank"
        ),
        schema="public",
    )


def downgrade() -> None:
    op.drop_table("companies", schema="public")
