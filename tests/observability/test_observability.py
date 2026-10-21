import asyncio
from io import StringIO
import json
import logging
from types import SimpleNamespace
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import generate_latest
import pytest
from starlette.responses import JSONResponse

from workwise.core import observability
from workwise.core.observability import HttpMetrics, ObservabilityMiddleware, RequestJsonFormatter, request_id_context


@pytest.fixture
def telemetry(monkeypatch):
    stream = StringIO()
    logger = logging.Logger("test_requests", level=logging.INFO)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RequestJsonFormatter())
    logger.addHandler(handler)
    monkeypatch.setattr(observability, "request_logger", logger)
    return HttpMetrics(), stream


async def invoke(app, messages, path="/jobs/123", method="GET", route="/jobs/{job_id}"):
    scope = {
        "type": "http", "method": method, "path": path, "http_version": "1.1",
        "headers": [(b"authorization", b"Bearer private-test-token"),
                    (b"cookie", b"private-cookie"), (b"x-request-id", b"client-id-must-not-be-trusted")],
        "query_string": b"password=private-query", "state": {},
    }
    if route is not None:
        scope["route"] = SimpleNamespace(path=route)

    async def receive():
        return {"type": "http.request", "body": b"private-request-body", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    return scope


def records(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_request_id_log_and_metrics_exclude_sensitive_input(telemetry):
    metrics, stream = telemetry

    async def endpoint(scope, receive, send):
        assert request_id_context.get() == scope["state"]["request_id"]
        await JSONResponse({"ok": True}, headers={"X-Request-ID": "untrusted-app-value"})(scope, receive, send)

    middleware = ObservabilityMiddleware(endpoint, metrics)
    messages = []
    asyncio.run(invoke(middleware, messages))
    ids = [value.decode() for name, value in messages[0]["headers"] if name.lower() == b"x-request-id"]
    assert len(ids) == 1 and UUID(ids[0]).version == 4
    event, = records(stream)
    assert event["request_id"] == ids[0]
    assert event["route"] == "/jobs/{job_id}"
    assert event["method"] == "GET" and event["status"] == 200
    assert event["duration_ms"] >= 0 and event["response_complete"]
    assert event["error_type"] is None and event["timestamp"].endswith("+00:00")
    assert metrics.requests.labels("GET", "/jobs/{job_id}", "200")._value.get() == 1
    output = stream.getvalue() + generate_latest(metrics.registry).decode()
    for secret in ("private-test-token", "private-cookie", "private-query", "private-request-body",
                   "client-id-must-not-be-trusted", "untrusted-app-value", "/jobs/123"):
        assert secret not in output
    assert request_id_context.get() is None


def test_route_and_unknown_method_cardinality_is_bounded(telemetry):
    metrics, stream = telemetry

    async def endpoint(scope, receive, send):
        await JSONResponse({}, status_code=404)(scope, receive, send)

    middleware = ObservabilityMiddleware(endpoint, metrics)
    for path, method in (("/private-one", "CUSTOM-ONE"), ("/private-two", "CUSTOM-TWO")):
        asyncio.run(invoke(middleware, [], path=path, method=method, route=None))
    assert metrics.requests.labels("OTHER", "__unmatched__", "404")._value.get() == 2
    assert {record["route"] for record in records(stream)} == {"__unmatched__"}
    assert "private-one" not in generate_latest(metrics.registry).decode()
    assert "CUSTOM-ONE" not in stream.getvalue()


def test_streaming_body_is_untouched_and_counted_once(telemetry):
    metrics, stream = telemetry

    async def endpoint(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        await send({"type": "http.response.body", "body": b"second", "more_body": False})

    messages = []
    asyncio.run(invoke(ObservabilityMiddleware(endpoint, metrics), messages))
    assert [message.get("body") for message in messages[1:]] == [b"first", b"second"]
    assert len(records(stream)) == 1
    assert metrics.requests.labels("GET", "/jobs/{job_id}", "200")._value.get() == 1
    assert metrics.registry.get_sample_value("workwise_http_request_duration_seconds_count",
                                             {"method": "GET", "route": "/jobs/{job_id}"}) == 1


def test_unhandled_error_has_generic_500_and_correlated_error_record(telemetry):
    metrics, stream = telemetry
    api = FastAPI()
    api.add_middleware(ObservabilityMiddleware, metrics=metrics)

    @api.get("/boom")
    def boom():
        raise RuntimeError("private exception data must not be logged")

    with TestClient(api) as client:
        response = client.get("/boom")
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    event, = records(stream)
    assert event["request_id"] == response.headers["x-request-id"]
    assert event["status"] == 500 and event["error_type"] == "RuntimeError"
    assert event["level"] == "ERROR"
    assert "private exception data" not in stream.getvalue() + response.text
    assert metrics.errors.labels("GET", "/boom")._value.get() == 1
    assert metrics.requests.labels("GET", "/boom", "500")._value.get() == 1


def test_exception_after_response_start_does_not_send_a_second_status(telemetry):
    metrics, stream = telemetry

    async def endpoint(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        raise RuntimeError("private streaming exception")

    messages = []
    with pytest.raises(RuntimeError):
        asyncio.run(invoke(ObservabilityMiddleware(endpoint, metrics), messages))
    assert len([message for message in messages if message["type"] == "http.response.start"]) == 1
    event, = records(stream)
    assert event["status"] == 200 and not event["response_complete"]
    assert event["error_type"] == "RuntimeError"
    assert "private streaming exception" not in stream.getvalue()
    assert metrics.errors.labels("GET", "/jobs/{job_id}")._value.get() == 1
    assert request_id_context.get() is None


def test_concurrent_requests_keep_separate_contexts(telemetry):
    metrics, stream = telemetry

    async def endpoint(scope, receive, send):
        before = request_id_context.get()
        await asyncio.sleep(0)
        assert before == request_id_context.get() == scope["state"]["request_id"]
        await JSONResponse({})(scope, receive, send)

    async def run():
        first, second = [], []
        middleware = ObservabilityMiddleware(endpoint, metrics)
        await asyncio.gather(invoke(middleware, first), invoke(middleware, second, path="/jobs/456"))
        return first, second

    first, second = asyncio.run(run())
    ids = {dict(messages[0]["headers"])[b"x-request-id"].decode() for messages in (first, second)}
    assert len(ids) == 2 and {event["request_id"] for event in records(stream)} == ids
    assert request_id_context.get() is None


def test_cancellation_resets_context_and_does_not_manufacture_a_response(telemetry):
    metrics, stream = telemetry

    async def endpoint(scope, receive, send):
        raise asyncio.CancelledError()

    messages = []
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(invoke(ObservabilityMiddleware(endpoint, metrics), messages))
    assert messages == [] and request_id_context.get() is None
    assert records(stream)[0]["status"] == 499


@pytest.mark.parametrize("scope_type", ["websocket", "lifespan"])
def test_non_http_scopes_are_passed_through(telemetry, scope_type):
    metrics, stream = telemetry
    calls = []

    async def endpoint(scope, receive, send):
        calls.append(scope["type"])

    asyncio.run(ObservabilityMiddleware(endpoint, metrics)({"type": scope_type}, None, None))
    assert calls == [scope_type]
    assert stream.getvalue() == ""


def test_metrics_require_admin_and_expose_prometheus_text(anonymous_client, db_client, auth_user, db_session):
    # Use an explicit missing header; db_client and anonymous_client share this test client.
    token = db_client.headers.pop("Authorization")
    assert anonymous_client.get("/metrics").status_code == 401
    db_client.headers["Authorization"] = token
    assert db_client.get("/metrics").status_code == 403
    auth_user.role = "admin"
    db_session.commit()
    db_client.get("/health")
    response = db_client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["cache-control"] == "no-store"
    assert "workwise_http_requests_total" in response.text
    assert 'route="/health"' in response.text
    assert "workwise_http_request_duration_seconds_bucket" in response.text
    assert token not in response.text
    assert UUID(response.headers["x-request-id"]).version == 4
    auth_user.role = "member"
    db_session.commit()
    assert db_client.get("/metrics").status_code == 403


@pytest.mark.parametrize("path,status", [("/health", 200), ("/jobs/abc", 422), ("/jobs/2147483647", 404),
                                        ("/not-a-route", 404), ("/auth/me", 401)])
def test_request_ids_on_success_and_handled_errors(anonymous_client, path, status):
    response = anonymous_client.get(path, headers={"X-Request-ID": "do-not-echo"})
    assert response.status_code == status
    assert UUID(response.headers["x-request-id"]).version == 4
