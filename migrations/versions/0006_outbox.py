"""Store job creation events atomically with jobs.

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        schema="public",
    )
    op.create_index("ix_outbox_pending", "outbox_events", ["created_at"],
                    postgresql_where=sa.text("published_at IS NULL"), schema="public")


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("ix_outbox_pending", table_name="outbox_events", schema="public")
    op.drop_table("outbox_events", schema="public")
