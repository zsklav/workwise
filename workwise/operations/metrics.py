"""Service-only scraping credentials and bounded database aggregate snapshots."""
from functools import lru_cache
import secrets
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CollectorRegistry, Gauge, generate_latest
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from workwise.core.paths import ROOT_DIR


class MonitoringSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", extra="ignore", hide_input_in_errors=True)
    metrics_token: SecretStr | None = Field(default=None, min_length=32)

    @field_validator("metrics_token")
    @classmethod
    def validate_token(cls, value):
        if value is not None:
            raw = value.get_secret_value()
            if raw.startswith("replace_") or not raw.isascii() or any(c.isspace() for c in raw):
                raise ValueError("Use a random ASCII METRICS_TOKEN without whitespace")
        return value


@lru_cache
def settings():
    return MonitoringSettings()


bearer = HTTPBearer(auto_error=False)


def require_scrape_token(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    expected = settings().metrics_token
    if expected is None:
        raise HTTPException(404, "Monitoring not configured")
    if credentials is None or not secrets.compare_digest(credentials.credentials.encode(), expected.get_secret_value().encode()):
        raise HTTPException(401, "Invalid monitoring credentials", headers={"WWW-Authenticate": "Bearer"})


Scraper = Annotated[None, Depends(require_scrape_token)]


def inspection_engine(url):
    # A separate small pool keeps a saturated business pool from blocking scrapes.
    return create_engine(url, pool_size=1, max_overflow=0, pool_timeout=1, pool_pre_ping=True,
                         connect_args={"connect_timeout": 2, "options": "-c statement_timeout=2000 -c lock_timeout=1000"})


QUERIES = {
    "outbox": """SELECT count(*) FILTER (WHERE published_at IS NULL) AS pending,
        COALESCE(GREATEST(EXTRACT(EPOCH FROM (now() - min(created_at) FILTER (WHERE published_at IS NULL))), 0), 0) AS oldest_seconds,
        count(*) FILTER (WHERE published_at IS NOT NULL) AS published FROM outbox_events""",
    "inbox": """SELECT count(*) FILTER (WHERE read_at IS NULL) AS unread,
        count(*) FILTER (WHERE read_at IS NOT NULL) AS read FROM notifications""",
}


def database_metrics(database, kind):
    query = QUERIES[kind]
    registry = CollectorRegistry()
    available = Gauge("workwise_database_up", "Whether the aggregate database query succeeded", registry=registry)
    try:
        with database.begin() as connection:
            values = connection.execute(text(query)).mappings().one()
    except SQLAlchemyError:
        available.set(0)
    else:
        available.set(1)
        for name, value in values.items():
            metric = f"workwise_{kind}_{name}"
            Gauge(metric, f"Current {kind} {name}", registry=registry).set(value)
    # On failure unknown aggregates are absent, never falsely exported as zero.
    return generate_latest(registry)
