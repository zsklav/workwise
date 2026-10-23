"""Atomic client retries and versioned job applications."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table("idempotency_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("public.users.id", name="fk_idempotency_user", ondelete="CASCADE"), nullable=False),
        sa.Column("scope", sa.String(100), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_body", JSONB),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "scope", "key_hash", name="uq_idempotency_actor_scope_key"), schema="public")
    op.create_index("ix_idempotency_records_expires_at", "idempotency_records", ["expires_at"], schema="public")
    op.create_table("job_applications",
        sa.Column("id", sa.Integer(), sa.Identity(always=True), primary_key=True),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("public.jobs.id", name="fk_application_job", ondelete="RESTRICT"), nullable=False),
        sa.Column("applicant_id", sa.Integer(), sa.ForeignKey("public.users.id", name="fk_application_user", ondelete="RESTRICT"), nullable=False),
        sa.Column("cover_letter", sa.Text()),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'applied'")),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("job_id", "applicant_id", name="uq_application_job_applicant"),
        sa.CheckConstraint("status IN ('applied', 'shortlisted', 'rejected', 'hired')", name="ck_application_status"),
        sa.CheckConstraint("version > 0", name="ck_application_version_positive"),
        sa.CheckConstraint("cover_letter IS NULL OR length(cover_letter) <= 4000", name="ck_application_cover_length"), schema="public")
    op.create_index("ix_applications_applicant_id_id", "job_applications", ["applicant_id", "id"], schema="public")
    op.create_index("ix_applications_job_id_id", "job_applications", ["job_id", "id"], schema="public")


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("job_applications", schema="public")
    op.drop_table("idempotency_records", schema="public")
