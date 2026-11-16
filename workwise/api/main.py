"""FastAPI assembly: middleware, error handling and route registration."""
import os

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError

from workwise.api.routes import (applications, auth, companies, jobs, notifications, operations,
                                 password_reset, resumes)
from workwise.api.dependencies import DbSession
from workwise.core.auth_limits import throttle
from workwise.core.observability import ObservabilityMiddleware
from workwise.core.security_headers import SecurityHeadersMiddleware
from workwise.core.tracing import TracingMiddleware, configure_tracing

UNLIMITED_PATHS = {"/health", "/ready", "/metrics"}


def limit_requests(request: Request, db: DbSession) -> None:
    """Per-IP budget for every route except probes and the metrics scrape."""
    if request.url.path not in UNLIMITED_PATHS:
        throttle(db, request, "api")


configure_tracing("workwise-api")
app = FastAPI(title="WorkWise API", dependencies=[Depends(limit_requests)])
app.add_middleware(ObservabilityMiddleware)
app.add_middleware(TracingMiddleware)
# Browser origins are opt-in: CORS_ORIGINS="https://app.example.com,https://other.example.com"
_origins = [item.strip() for item in os.environ.get("CORS_ORIGINS", "").split(",") if item.strip()]
if _origins:
    app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                       allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "If-Match"],
                       expose_headers=["X-Total-Count", "X-Request-ID", "ETag"], max_age=600)
app.add_middleware(SecurityHeadersMiddleware)
# Probes and metrics stay unversioned. Everything else lives under /v1; the unprefixed paths
# remain as hidden aliases so existing clients keep working.
app.include_router(operations.router)
for router in (companies.router, auth.router, password_reset.router, resumes.router, notifications.router,
               jobs.router, applications.router):
    app.include_router(router, prefix="/v1")
    app.include_router(router, include_in_schema=False)


@app.exception_handler(RequestValidationError)
async def validation_error(request, error: RequestValidationError):
    if ("auth" in request.url.path.split("/")[:3] or "applications" in request.url.path.split("/")
            or any("idempotency-key" in item["loc"] for item in error.errors())):
        # Validation errors can otherwise echo raw credentials in their input.
        errors = [{key: value for key, value in item.items() if key not in {"input", "ctx"}}
                  for item in error.errors()]
        error = RequestValidationError(errors)
    return await request_validation_exception_handler(request, error)
