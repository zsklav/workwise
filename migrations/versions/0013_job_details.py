"""Add salary range, remote flag and job type to jobs."""
from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("jobs", sa.Column("salary_min", sa.Integer(), nullable=True), schema="public")
    op.add_column("jobs", sa.Column("salary_max", sa.Integer(), nullable=True), schema="public")
    op.add_column("jobs", sa.Column("remote", sa.Boolean(), nullable=False, server_default=sa.text("false")), schema="public")
    op.add_column("jobs", sa.Column("job_type", sa.Text(), nullable=True), schema="public")
    op.create_check_constraint("ck_jobs_salary_nonnegative", "jobs", "salary_min >= 0 AND salary_max >= 0", schema="public")
    op.create_check_constraint("ck_jobs_salary_range", "jobs",
                               "salary_min IS NULL OR salary_max IS NULL OR salary_max >= salary_min", schema="public")
    op.create_check_constraint("ck_jobs_job_type", "jobs",
                               "job_type IN ('full_time', 'part_time', 'contract', 'internship')", schema="public")


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    for name in ("ck_jobs_job_type", "ck_jobs_salary_range", "ck_jobs_salary_nonnegative"):
        op.drop_constraint(name, "jobs", type_="check", schema="public")
    for column in ("job_type", "remote", "salary_max", "salary_min"):
        op.drop_column("jobs", column, schema="public")
