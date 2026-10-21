
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from workwise.core.paths import ROOT_DIR


def migration_config(connection):
    config = Config(str(ROOT_DIR / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


def test_company_backfill_preserves_rows_and_reuses_exact_names(db_connection):
    # DDL and data changes are inside this test's rolled-back outer transaction.
    config = migration_config(db_connection)
    command.downgrade(config, "0003")
    existing_id = db_connection.scalar(text("""
        INSERT INTO public.companies (name, description)
        VALUES ('WorkWise', 'Keep existing company details') RETURNING id
    """))
    names = ["WorkWise", "WorkWise", "workwise", "O'Reilly %_ Labs", " WorkWise "]
    for number, name in enumerate(names):
        db_connection.execute(text("""
            INSERT INTO public.jobs (title, company, location, description)
            VALUES (:title, :company, 'Remote', :description)
        """), {"title": f"Legacy {number}", "company": name, "description": None if number == 0 else "Keep text"})
    before = db_connection.execute(text("SELECT * FROM public.jobs ORDER BY id")).mappings().all()

    command.upgrade(config, "0004")

    after = db_connection.execute(text("""
        SELECT j.id, j.title, c.name AS company, j.location, j.description
        FROM public.jobs j JOIN public.companies c ON c.id = j.company_id ORDER BY j.id
    """)).mappings().all()
    assert [dict(row) for row in after] == [dict(row) for row in before]
    assert db_connection.scalar(text("SELECT count(*) FROM public.companies")) == 4
    assert db_connection.scalar(text("SELECT company_id FROM public.jobs ORDER BY id LIMIT 1")) == existing_id
    assert db_connection.scalar(text("SELECT description FROM public.companies WHERE id = :id"), {"id": existing_id}) == "Keep existing company details"
    assert "company" not in {column["name"] for column in inspect(db_connection).get_columns("jobs", schema="public")}

    command.downgrade(config, "0003")
    restored = db_connection.execute(text("SELECT * FROM public.jobs ORDER BY id")).mappings().all()
    assert [dict(row) for row in restored] == [dict(row) for row in before]
    assert db_connection.scalar(text("SELECT count(*) FROM public.companies")) == 4


@pytest.mark.parametrize("name", ["", "   ", "\t\n", "x" * 201])
def test_invalid_legacy_names_abort_backfill_without_schema_or_data_changes(db_connection, name):
    config = migration_config(db_connection)
    command.downgrade(config, "0003")
    db_connection.execute(text("""
        INSERT INTO public.jobs (title, company, location)
        VALUES ('Invalid legacy name', :name, 'Remote')
    """), {"name": name})

    with pytest.raises(DBAPIError, match="invalid legacy job company names"):
        with db_connection.begin_nested():
            command.upgrade(config, "0004")

    assert db_connection.scalar(text("SELECT version_num FROM public.alembic_version")) == "0003"
    assert db_connection.scalar(text("SELECT company FROM public.jobs")) == name
    assert db_connection.scalar(text("SELECT count(*) FROM public.companies")) == 0
    assert "company_id" not in {column["name"] for column in inspect(db_connection).get_columns("jobs", schema="public")}


def test_user_migration_preserves_legacy_records_without_granting_ownership(db_connection):
    config = migration_config(db_connection)
    command.downgrade(config, "0004")
    company_id = db_connection.scalar(text("""
        INSERT INTO public.companies (name, description)
        VALUES ('Legacy auth company', 'Preserve description') RETURNING id
    """))
    db_connection.execute(text("""
        INSERT INTO public.jobs (title, company_id, location, description)
        VALUES ('Legacy auth job', :company_id, 'Remote', 'Preserve job')
    """), {"company_id": company_id})
    before = [dict(row) for row in db_connection.execute(text("SELECT * FROM public.jobs ORDER BY id")).mappings()]

    command.upgrade(config, "0005")

    assert db_connection.scalar(text("SELECT count(*) FROM public.users")) == 0
    stored = db_connection.execute(text("SELECT * FROM public.companies")).mappings().one()
    assert dict(stored) == {"id": company_id, "name": "Legacy auth company",
                            "description": "Preserve description", "owner_id": None}
    assert [dict(row) for row in db_connection.execute(text("SELECT * FROM public.jobs ORDER BY id")).mappings()] == before

    command.downgrade(config, "0004")
    assert not inspect(db_connection).has_table("users", schema="public")
    assert [dict(row) for row in db_connection.execute(text("SELECT * FROM public.jobs ORDER BY id")).mappings()] == before
