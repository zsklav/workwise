"""Explicit W3C context propagation without bodies, credentials or exception text."""
from contextlib import contextmanager
import os

from opentelemetry import trace
from opentelemetry.trace import SpanKind, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

propagator = TraceContextTextMapPropagator()


def configure_tracing(service_name):
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    if not endpoint:
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, timeout=3),
                                                   schedule_delay_millis=500, max_queue_size=2048))
    trace.set_tracer_provider(provider)


def tracer():
    return trace.get_tracer("workwise")


def trace_headers(headers):
    return {key: headers[key] for key in ("traceparent", "tracestate")
            if isinstance((headers or {}).get(key), str)}


def capture_context():
    carrier = {}
    propagator.inject(carrier)
    return carrier


def trace_id():
    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else None


def failed(error_type):
    current = trace.get_current_span()
    current.set_status(StatusCode.ERROR)
    current.set_attribute("error.type", error_type)


@contextmanager
def span(name, kind=SpanKind.INTERNAL, carrier=None):
    parent = propagator.extract(trace_headers(carrier)) if carrier is not None else None
    with tracer().start_as_current_span(name, kind=kind, context=parent,
                                        record_exception=False, set_status_on_exception=False) as current:
        try:
            yield current
        except Exception as error:
            current.set_status(StatusCode.ERROR)
            current.set_attribute("error.type", type(error).__name__)
            raise


class TracingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        incoming = {key.decode("latin-1").lower(): value.decode("latin-1")
                    for key, value in scope["headers"] if key.lower() in (b"traceparent", b"tracestate")}
        method = scope.get("method", "OTHER")
        method = method if method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"} else "OTHER"
        status = 500
        with span("HTTP", SpanKind.SERVER, incoming) as current:
            async def traced_send(message):
                nonlocal status
                if message["type"] == "http.response.start":
                    status = message["status"]
                    identity = trace_id()
                    if identity:
                        message = {**message, "headers": [(key, value) for key, value in message.get("headers", [])
                                                          if key.lower() != b"x-trace-id"] + [(b"x-trace-id", identity.encode())]}
                await send(message)
            try:
                await self.app(scope, receive, traced_send)
            finally:
                route = getattr(scope.get("route"), "path", "__unmatched__")
                current.update_name(method + " " + route)
                current.set_attribute("http.request.method", method)
                current.set_attribute("http.route", route)
                current.set_attribute("http.response.status_code", status)
                if status >= 500:
                    current.set_status(StatusCode.ERROR)
