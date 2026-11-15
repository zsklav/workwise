"""Soft-delete jobs: a deleted job keeps its row, applications and history."""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("jobs", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True), schema="public")


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Deleted rows would reappear as live jobs, so remove them first. Jobs with
    # applications cannot be removed; downgrade stops there instead of losing data.
    op.execute("DELETE FROM public.jobs WHERE deleted_at IS NOT NULL")
    op.drop_column("jobs", "deleted_at", schema="public")
