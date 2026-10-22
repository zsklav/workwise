"""Replace the single-column company index with company and ID ordering."""
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_index("ix_jobs_company_id_id", "jobs", ["company_id", "id"], schema="public")
    op.drop_index("ix_jobs_company_id", table_name="jobs", schema="public")


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_index("ix_jobs_company_id", "jobs", ["company_id"], schema="public")
    op.drop_index("ix_jobs_company_id_id", table_name="jobs", schema="public")
