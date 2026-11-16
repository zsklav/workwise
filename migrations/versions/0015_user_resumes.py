"""One stored resume per user; the file lives in local storage, the row tracks it."""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "user_resumes",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("public.users.id", name="fk_resume_user", ondelete="CASCADE"), primary_key=True),
        sa.Column("storage_key", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("size_bytes > 0", name="ck_resume_size_positive"),
        schema="public",
    )


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("user_resumes", schema="public")
