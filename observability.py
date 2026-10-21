"""Bounded HTTP metrics and request logs that exclude request data."""

import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
import sys
from time import perf_counter
from uuid import uuid4

from prometheus_client import CollectorRegistry, Counter, Histogram
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)


class RequestJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Do not format arbitrary messages, exception strings, headers, or bodies.
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "event": "http_request",
        }
        for key in ("request_id", "method", "route", "status", "duration_ms",
                    "response_started", "response_complete", "error_type"):
            event[key] = getattr(record, key, None)
        return json.dumps(event, separators=(",", ":"))


request_logger = logging.getLogger("workwise.requests")
request_logger.setLevel(logging.INFO)
request_logger.propagate = False
if not request_logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RequestJsonFormatter())
    request_logger.addHandler(handler)


class HttpMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "workwise_http_requests_total", "HTTP requests observed by WorkWise",
            ["method", "route", "status"], registry=self.registry,
        )
        self.duration = Histogram(
            "workwise_http_request_duration_seconds", "Time until the HTTP ASGI call completes",
            ["method", "route"], registry=self.registry,
        )
        self.errors = Counter(
            "workwise_http_unhandled_errors_total", "Unhandled HTTP application errors",
            ["method", "route"], registry=self.registry,
        )


http_metrics = HttpMetrics()


class ObservabilityMiddleware:
    def __init__(self, app: ASGIApp, metrics: HttpMetrics = http_metrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid4().hex
        context_token = request_id_context.set(request_id)
        scope.setdefault("state", {})["request_id"] = request_id
        started = perf_counter()
        status = 500
        response_started = False
        response_complete = False
        error_type = None

        async def observed_send(message: Message) -> None:
            nonlocal status, response_started, response_complete
            if message["type"] == "http.response.start":
                status = message["status"]
                response_started = True
                headers = [(name, value) for name, value in message.get("headers", [])
                           if name.lower() != b"x-request-id"]
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": headers}
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                response_complete = True
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        except asyncio.CancelledError:
            if not response_started:
                status = 499  # Operational label only; no HTTP response was sent.
            raise
        except Exception as error:
            error_type = type(error).__name__
            if response_started:
                # A streaming response cannot safely be replaced with a second status.
                raise
            await JSONResponse({"detail": "Internal server error"}, status_code=500)(
                scope, receive, observed_send,
            )
        finally:
            try:
                elapsed = max(0.0, perf_counter() - started)
                method = scope.get("method", "OTHER")
                if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"}:
                    method = "OTHER"
                route = getattr(scope.get("route"), "path", "__unmatched__")
                self.metrics.requests.labels(method, route, str(status)).inc()
                self.metrics.duration.labels(method, route).observe(elapsed)
                if error_type:
                    self.metrics.errors.labels(method, route).inc()
                request_logger.log(logging.ERROR if error_type or status >= 500 else logging.INFO,
                    "http_request", extra={
                        "request_id": request_id_context.get(), "method": method, "route": route,
                        "status": status, "duration_ms": round(elapsed * 1000, 3),
                        "response_started": response_started, "response_complete": response_complete,
                        "error_type": error_type,
                    })
            finally:
                request_id_context.reset(context_token)
