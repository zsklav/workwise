"""Apply migrations and explicit runtime grants using the migration role."""

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from workwise.core.config import MigrationSettings

from workwise.core.paths import ROOT_DIR

GRANTS = (
    "SELECT, INSERT, UPDATE, DELETE ON TABLE public.jobs, public.companies",
    "SELECT, INSERT ON TABLE public.users",
    "UPDATE (token_version, password_hash, email) ON TABLE public.users",
    "SELECT, INSERT, UPDATE ON TABLE public.password_reset_tokens",
    "SELECT, INSERT, UPDATE, DELETE ON TABLE public.user_resumes",
    "SELECT, INSERT, UPDATE ON TABLE public.auth_refresh_tokens, public.auth_rate_limits",
    "SELECT, INSERT, UPDATE ON TABLE public.idempotency_records, public.job_applications",
    "SELECT ON TABLE public.alembic_version",
    "SELECT, INSERT, UPDATE ON TABLE public.outbox_events",
)


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
            if settings.db_user == settings.migration_db_user:
                # One shared login (managed hosts such as Supabase): it already owns every table,
                # and a pooler login name may not even be a real database role.
                print("Runtime and migration login are the same; no separate grants needed.")
            else:
                role = engine.dialect.identifier_preparer.quote(settings.db_user)
                for grant in GRANTS:
                    connection.execute(text(f"GRANT {grant} TO {role}"))
        print("Migrations and runtime grants completed.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
