"""Add optimistic concurrency revisions without changing existing business fields."""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    for table in ("jobs", "companies"):
        op.add_column(table, sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")), schema="public")
        op.create_check_constraint(f"ck_{table}_version_positive", table, "version > 0", schema="public")


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    for table in ("companies", "jobs"):
        op.drop_constraint(f"ck_{table}_version_positive", table, type_="check", schema="public")
        op.drop_column(table, "version", schema="public")
