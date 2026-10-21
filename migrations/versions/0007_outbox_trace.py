"""Preserve W3C trace context across the durable outbox boundary."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("outbox_events", sa.Column("trace_context", JSONB(), nullable=True), schema="public")


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("outbox_events", "trace_context", schema="public")
