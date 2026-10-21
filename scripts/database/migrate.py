"""Apply migrations and explicit runtime grants using the migration role."""

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from workwise.core.config import MigrationSettings

from workwise.core.paths import ROOT_DIR


def main() -> None:
    settings = MigrationSettings()
    url = URL.create(
        "postgresql+psycopg", username=settings.migration_db_user,
        password=settings.migration_db_password.get_secret_value(), host=settings.db_host,
        port=settings.db_port, database=settings.db_name,
    )
    engine = create_engine(url, connect_args={"connect_timeout": 5})
    try:
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            config = Config(str(ROOT_DIR / "alembic.ini"))
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            role = engine.dialect.identifier_preparer.quote(settings.db_user)
            connection.execute(text("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.jobs, public.companies TO " + role))
            connection.execute(text("GRANT SELECT, INSERT ON TABLE public.users TO " + role))
            connection.execute(text("GRANT SELECT ON TABLE public.alembic_version TO " + role))
            connection.execute(text("GRANT SELECT, INSERT, UPDATE ON TABLE public.outbox_events TO " + role))
        print("Migrations and runtime grants completed.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
