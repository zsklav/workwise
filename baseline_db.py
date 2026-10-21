"""Verify the existing jobs table and record its initial Alembic revision."""

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import URL

from models import Base, Job
from settings import MigrationSettings


def main() -> None:
    settings = MigrationSettings()
    url = URL.create(
        "postgresql+psycopg",
        username=settings.migration_db_user,
        password=settings.migration_db_password.get_secret_value(),
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
    )
    engine = create_engine(url, connect_args={"connect_timeout": 5})

    try:
        with engine.begin() as connection:
            identity = connection.execute(
                text("SELECT current_database(), current_user")
            ).one()
            if tuple(identity) != (settings.db_name, settings.migration_db_user):
                raise RuntimeError("Unexpected database or migration role.")

            owner = connection.scalar(text(
                "SELECT pg_get_userbyid(relowner) FROM pg_class "
                "WHERE oid = 'public.jobs'::regclass"
            ))
            can_create = connection.scalar(text(
                "SELECT has_schema_privilege(current_user, 'public', 'CREATE')"
            ))
            if owner != settings.migration_db_user or not can_create:
                raise RuntimeError("Migration role needs jobs ownership and schema CREATE.")

            # Briefly block writes/DDL while checking the schema and existing rows.
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            connection.execute(text("LOCK TABLE public.jobs IN SHARE MODE"))
            before = connection.execute(
                select(Job.__table__).order_by(Job.id)
            ).all()

            migration_context = MigrationContext.configure(connection, opts={
                "include_schemas": True,
                "compare_type": True,
                "compare_server_default": True,
            })
            differences = compare_metadata(migration_context, Base.metadata)
            if differences:
                raise RuntimeError(f"Schema mismatch; baseline not stamped: {differences!r}")

            revisions = migration_context.get_current_heads()
            if revisions not in ((), ("0001",)):
                raise RuntimeError("Unexpected migration history; baseline not stamped.")

            config = Config(str(Path(__file__).with_name("alembic.ini")))
            config.attributes["connection"] = connection
            command.stamp(config, "0001")

            after = connection.execute(
                select(Job.__table__).order_by(Job.id)
            ).all()
            if before != after:
                raise RuntimeError("Rows changed during baseline; transaction rolled back.")
            if migration_context.get_current_heads() != ("0001",):
                raise RuntimeError("Baseline verification failed; transaction rolled back.")

        print(f"Database: {identity[0]}; migration role: {identity[1]}")
        print("Schema matched; baseline 0001 recorded.")
        print(f"Existing jobs preserved: {len(before)}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
