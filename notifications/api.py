"""Internal HTTP service. Public users enter through the main authenticated API."""
from functools import lru_cache
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.exceptions import InvalidTokenError
from pydantic import Field, SecretStr
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from messaging.notification_store import NotificationSettings, store_engine
from notifications.contracts import NotificationRead, service_subject
from observability import ObservabilityMiddleware
from tracing import TracingMiddleware, configure_tracing


class Settings(NotificationSettings):
    notification_db_user: str = "notification_api"
    notification_db_password: SecretStr = Field(validation_alias="NOTIFICATION_API_DB_PASSWORD")
    notification_service_secret: SecretStr = Field(min_length=32)


@lru_cache
def settings():
    return Settings()


@lru_cache
def database():
    return store_engine(settings())


def get_connection():
    try:
        with database().begin() as connection:
            yield connection
    except SQLAlchemyError:
        raise HTTPException(503, "Notification database unavailable") from None


bearer = HTTPBearer(auto_error=False)


def current_subject(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    try:
        if credentials is None:
            raise InvalidTokenError()
        return service_subject(credentials.credentials, settings().notification_service_secret.get_secret_value())
    except (InvalidTokenError, ValueError, TypeError):
        raise HTTPException(401, "Invalid service credentials", headers={"WWW-Authenticate": "Bearer"}) from None


configure_tracing("workwise-notification-api")
app = FastAPI(title="WorkWise internal notifications", docs_url=None, redoc_url=None)
app.add_middleware(ObservabilityMiddleware)
app.add_middleware(TracingMiddleware)
Subject = Annotated[int, Depends(current_subject)]
Connection = Annotated[object, Depends(get_connection, scope="function")]
FIELDS = "event_id, job_id, company_id, message, occurred_at, created_at, read_at"


@app.get("/health")
def health():
    return {"status": "OK"}


@app.get("/ready")
def ready(connection: Connection):
    if tuple(connection.scalars(text("SELECT version FROM notification_schema_version"))) != (2,):
        raise HTTPException(503, "Notification migrations not ready")
    connection.execute(text("SELECT event_id FROM notifications LIMIT 0"))
    return {"status": "ready"}


@app.get("/notifications", response_model=list[NotificationRead])
def list_notifications(user: Subject, connection: Connection, response: Response,
                       limit: Annotated[int, Query(ge=1, le=100)] = 20,
                       offset: Annotated[int, Query(ge=0)] = 0, unread_only: bool = False):
    response.headers["Cache-Control"] = "no-store"
    return connection.execute(text(
        f"SELECT {FIELDS} FROM notifications WHERE recipient_id = :user "
        "AND (NOT :unread OR read_at IS NULL) ORDER BY created_at DESC, event_id DESC LIMIT :limit OFFSET :offset"
    ), {"user": user, "unread": unread_only, "limit": limit, "offset": offset}).mappings().all()


@app.patch("/notifications/{event_id}/read", response_model=NotificationRead)
def mark_read(event_id: UUID, user: Subject, connection: Connection, response: Response):
    row = connection.execute(text(
        f"UPDATE notifications SET read_at = COALESCE(read_at, now()) WHERE event_id = :event AND recipient_id = :user RETURNING {FIELDS}"
    ), {"event": event_id, "user": user}).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, "Notification not found")
    response.headers["Cache-Control"] = "no-store"
    return row
