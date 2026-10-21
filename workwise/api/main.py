"""FastAPI assembly: middleware, error handling and route registration."""
from fastapi import FastAPI
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError

from workwise.api.routes import auth, companies, jobs, notifications, operations
from workwise.core.observability import ObservabilityMiddleware
from workwise.core.tracing import TracingMiddleware, configure_tracing

configure_tracing("workwise-api")
app = FastAPI(title="WorkWise API")
app.add_middleware(ObservabilityMiddleware)
app.add_middleware(TracingMiddleware)
app.include_router(companies.router)
app.include_router(auth.router)
app.include_router(notifications.router)
app.include_router(operations.router)
app.include_router(jobs.router)


@app.exception_handler(RequestValidationError)
async def validation_error(request, error: RequestValidationError):
    if request.url.path.startswith("/auth/"):
        # Validation errors can otherwise echo raw credentials in their input.
        errors = [{key: value for key, value in item.items() if key not in {"input", "ctx"}}
                  for item in error.errors()]
        error = RequestValidationError(errors)
    return await request_validation_exception_handler(request, error)
