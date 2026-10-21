"""Authenticated gateway; notification storage remains owned by its service."""
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
import httpx
from pydantic import AnyHttpUrl, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from notifications.contracts import NotificationRead, service_token
from security import CurrentUser
from tracing import SpanKind, capture_context, span


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=Path(__file__).resolve().parents[1] / ".env",
                                     extra="ignore", hide_input_in_errors=True)
    notification_service_url: AnyHttpUrl | None = None
    notification_service_secret: SecretStr | None = None


def get_gateway():
    settings = Settings()
    if not settings.notification_service_url or not settings.notification_service_secret:
        raise HTTPException(503, "Notification service unavailable")
    with httpx.Client(base_url=str(settings.notification_service_url), timeout=httpx.Timeout(5, connect=2),
                      follow_redirects=False, trust_env=False) as client:
        yield client, settings.notification_service_secret.get_secret_value()


Gateway = Annotated[tuple, Depends(get_gateway)]
router = APIRouter(prefix="/notifications", tags=["notifications"])


def forward(gateway, user_id, method, path, params=None):
    client, secret = gateway
    try:
        with span("notifications.request", SpanKind.CLIENT):
            result = client.request(method, path, params=params,
                                    headers={**capture_context(), "Authorization": "Bearer " + service_token(user_id, secret)})
        if result.status_code == 404:
            raise HTTPException(404, "Notification not found")
        if result.status_code != 200:
            raise HTTPException(503, "Notification service unavailable")
        return result.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "Notification service unavailable") from None


@router.get("", response_model=list[NotificationRead])
def list_notifications(user: CurrentUser, gateway: Gateway, response: Response,
                       limit: Annotated[int, Query(ge=1, le=100)] = 20,
                       offset: Annotated[int, Query(ge=0)] = 0, unread_only: bool = False):
    data = forward(gateway, user.id, "GET", "/notifications",
                   {"limit": limit, "offset": offset, "unread_only": unread_only})
    try:
        result = [NotificationRead.model_validate(item) for item in data] if isinstance(data, list) else None
        if result is None:
            raise ValueError()
    except (ValidationError, ValueError, TypeError):
        raise HTTPException(503, "Notification service unavailable") from None
    response.headers["Cache-Control"] = "no-store"
    return result


@router.patch("/{event_id}/read", response_model=NotificationRead)
def mark_read(event_id: UUID, user: CurrentUser, gateway: Gateway, response: Response):
    data = forward(gateway, user.id, "PATCH", f"/notifications/{event_id}/read")
    try:
        result = NotificationRead.model_validate(data)
    except ValidationError:
        raise HTTPException(503, "Notification service unavailable") from None
    response.headers["Cache-Control"] = "no-store"
    return result
