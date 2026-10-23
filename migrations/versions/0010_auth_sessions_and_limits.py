"""Persistent refresh rotation, account revocation and shared authentication throttles."""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("users", sa.Column("token_version", sa.Integer(), nullable=False, server_default=sa.text("1")), schema="public")
    op.create_check_constraint("ck_users_token_version_positive", "users", "token_version > 0", schema="public")
    op.create_table("auth_refresh_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("public.users.id", name="fk_refresh_user", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("token_version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)), schema="public")
    op.create_index("ix_auth_refresh_tokens_user_id", "auth_refresh_tokens", ["user_id"], schema="public")
    op.create_index("ix_auth_refresh_tokens_expires_at", "auth_refresh_tokens", ["expires_at"], schema="public")
    op.create_table("auth_rate_limits",
        sa.Column("bucket_hash", sa.String(64), primary_key=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("attempts > 0", name="ck_auth_rate_attempts_positive"), schema="public")
    op.create_index("ix_auth_rate_limits_window_end", "auth_rate_limits", ["window_end"], schema="public")


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("auth_rate_limits", schema="public")
    op.drop_table("auth_refresh_tokens", schema="public")
    op.drop_constraint("ck_users_token_version_positive", "users", type_="check", schema="public")
    op.drop_column("users", "token_version", schema="public")
