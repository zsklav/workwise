"""Add users and company ownership; legacy companies remain unowned.

Revision ID: 0005
Revises: 0004
"""

from alembic import op
import sqlalchemy as sa


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), sa.Identity(always=True), nullable=False),
        sa.Column("username", sa.String(50), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default=sa.text("'member'")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.PrimaryKeyConstraint("id", name="users_pkey"),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.CheckConstraint("username ~ '^[a-z0-9_]{3,50}$'", name="ck_users_username"),
        sa.CheckConstraint("role IN ('member', 'admin')", name="ck_users_role"),
        schema="public",
    )
    op.add_column("companies", sa.Column("owner_id", sa.Integer(), nullable=True), schema="public")
    op.create_foreign_key(
        "fk_companies_owner_id_users", "companies", "users", ["owner_id"], ["id"],
        source_schema="public", referent_schema="public", ondelete="RESTRICT",
    )
    op.create_index("ix_companies_owner_id", "companies", ["owner_id"], schema="public")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("ix_companies_owner_id", table_name="companies", schema="public")
    op.drop_constraint("fk_companies_owner_id_users", "companies", type_="foreignkey", schema="public")
    op.drop_column("companies", "owner_id", schema="public")
    op.drop_table("users", schema="public")
