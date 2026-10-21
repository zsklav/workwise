"""The worker's own database: no imports from API database, models or auth."""
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from psycopg.types.json import Jsonb


class NotificationSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", hide_input_in_errors=True)
    notification_db_host: str = "notification-db"
    notification_db_name: str = "notifications"
    notification_db_user: str = "notification_worker"
    notification_db_password: SecretStr


class EventConflict(ValueError):
    pass


def store_engine(settings):
    return create_engine(URL.create("postgresql+psycopg", host=settings.notification_db_host,
                                   database=settings.notification_db_name, username=settings.notification_db_user,
                                   password=settings.notification_db_password.get_secret_value()),
                         pool_pre_ping=True, connect_args={"connect_timeout": 5, "options": "-c statement_timeout=5000 -c lock_timeout=5000"})


def record_notification(engine, event):
    payload = event.model_dump(mode="json")
    with engine.begin() as connection:
        inserted = connection.execute(text(
            "INSERT INTO notification_receipts(event_id, payload) VALUES (:id, :payload) "
            "ON CONFLICT (event_id) DO NOTHING RETURNING event_id"
        ), {"id": event.event_id, "payload": Jsonb(payload)}).scalar_one_or_none()
        if inserted is None:
            existing = connection.scalar(text("SELECT payload FROM notification_receipts WHERE event_id = :id"),
                                         {"id": event.event_id})
            if existing != payload:
                raise EventConflict("Event ID reused with different data")
    return inserted is not None
