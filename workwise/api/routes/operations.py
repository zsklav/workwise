"""Process/database readiness and protected HTTP metrics."""
from alembic.config import Config
from alembic.script import ScriptDirectory
from functools import lru_cache
from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from workwise.api.dependencies import DbSession
from workwise.core.observability import http_metrics
from workwise.core.paths import ROOT_DIR
from workwise.core.security import CurrentUser
from workwise.operations.metrics import Scraper, database_metrics, inspection_engine
from workwise.db.session import database_url

router = APIRouter()
schema_head = ScriptDirectory.from_config(Config(str(ROOT_DIR / "alembic.ini"))).get_current_head()


@lru_cache
def monitoring_database():
    return inspection_engine(database_url)


@router.get("/internal/metrics", include_in_schema=False)
def service_metrics(scraper: Scraper) -> Response:
    content = generate_latest(http_metrics.registry) + database_metrics(monitoring_database(), "outbox")
    return Response(content=content, media_type=CONTENT_TYPE_LATEST, headers={"Cache-Control": "no-store"})


@router.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "OK"}


@router.get("/metrics", response_class=PlainTextResponse, tags=["operations"])
def metrics(user: CurrentUser) -> Response:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return Response(content=generate_latest(http_metrics.registry), media_type=CONTENT_TYPE_LATEST,
                    headers={"Cache-Control": "no-store"})


@router.get("/ready")
def readiness(db: DbSession) -> dict[str, str]:
    try:
        revisions = tuple(db.scalars(text("SELECT version_num FROM public.alembic_version")))
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="Database not ready") from None
    if revisions != (schema_head,):
        raise HTTPException(status_code=503, detail="Database migrations not ready")
    return {"status": "ready"}
