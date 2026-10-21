"""Backfill companies and replace job company text with a required foreign key.

Revision ID: 0004
Revises: 0003
"""

from alembic import op
import sqlalchemy as sa


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def lock_tables() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("LOCK TABLE public.jobs, public.companies IN ACCESS EXCLUSIVE MODE")


def upgrade() -> None:
    lock_tables()
    # Validate before changing schema; never truncate or normalize existing names.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM public.jobs
                WHERE company IS NULL OR length(company) > 200
                   OR company !~ '[^[:space:]]'
            ) THEN
                RAISE EXCEPTION 'Cannot backfill companies: invalid legacy job company names'
                    USING HINT = 'Review empty, whitespace-only, or over-200-character names before retrying.';
            END IF;
        END $$;
    """)
    op.add_column("jobs", sa.Column("company_id", sa.Integer(), nullable=True), schema="public")
    op.execute("""
        INSERT INTO public.companies (name)
        SELECT DISTINCT company FROM public.jobs ORDER BY company
        ON CONFLICT (name) DO NOTHING
    """)
    op.execute("""
        UPDATE public.jobs AS j SET company_id = c.id
        FROM public.companies AS c WHERE c.name = j.company
    """)
    op.alter_column("jobs", "company_id", nullable=False, schema="public")
    op.create_foreign_key(
        "fk_jobs_company_id_companies", "jobs", "companies", ["company_id"], ["id"],
        source_schema="public", referent_schema="public", ondelete="RESTRICT",
    )
    op.create_index("ix_jobs_company_id", "jobs", ["company_id"], schema="public")
    op.drop_column("jobs", "company", schema="public")


def downgrade() -> None:
    lock_tables()
    op.add_column("jobs", sa.Column("company", sa.Text(), nullable=True), schema="public")
    op.execute("""
        UPDATE public.jobs AS j SET company = c.name
        FROM public.companies AS c WHERE c.id = j.company_id
    """)
    op.alter_column("jobs", "company", nullable=False, schema="public")
    op.drop_index("ix_jobs_company_id", table_name="jobs", schema="public")
    op.drop_constraint("fk_jobs_company_id_companies", "jobs", type_="foreignkey", schema="public")
    op.drop_column("jobs", "company_id", schema="public")
    # Keep Company rows, including those added by the backfill.
