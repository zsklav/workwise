"""Optional account email and single-use password reset tokens."""
from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("users", sa.Column("email", sa.String(254), nullable=True), schema="public")
    op.create_unique_constraint("uq_users_email", "users", ["email"], schema="public")
    op.create_check_constraint("ck_users_email_lowercase", "users", "email = lower(email)", schema="public")
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("public.users.id", name="fk_reset_user", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        schema="public",
    )
    op.create_index("ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"], schema="public")
    op.create_index("ix_password_reset_tokens_expires_at", "password_reset_tokens", ["expires_at"], schema="public")


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("password_reset_tokens", schema="public")
    op.drop_constraint("ck_users_email_lowercase", "users", type_="check", schema="public")
    op.drop_constraint("uq_users_email", "users", type_="unique", schema="public")
    op.drop_column("users", "email", schema="public")
