"""Upgrade notification state and grant separate worker/API permissions."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from psycopg import sql
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from notifications.migration import upgrade_schema


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)
    notification_db_host: str = "notification-db"
    notification_admin_password: SecretStr
    notification_api_db_password: SecretStr


def main():
    settings = Settings()
    engine = create_engine(URL.create("postgresql+psycopg", host=settings.notification_db_host,
                                     username="postgres", database="notifications",
                                     password=settings.notification_admin_password.get_secret_value()),
                           connect_args={"connect_timeout": 5})
    try:
        with engine.begin() as connection:
            upgrade_schema(connection)
            if not connection.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='notification_api')")):
                connection.connection.driver_connection.execute(sql.SQL("CREATE ROLE notification_api LOGIN PASSWORD {}").format(
                    sql.Literal(settings.notification_api_db_password.get_secret_value())))
            connection.execute(text("GRANT CONNECT ON DATABASE notifications TO notification_api"))
            connection.execute(text("GRANT USAGE ON SCHEMA public TO notification_api"))
            connection.execute(text("GRANT SELECT ON notifications, notification_schema_version TO notification_api"))
            connection.execute(text("GRANT UPDATE (read_at) ON notifications TO notification_api"))
            connection.execute(text("GRANT SELECT, INSERT ON notifications TO notification_worker"))
        print("Notification migration v2 and separate runtime grants completed.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
