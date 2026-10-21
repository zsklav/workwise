from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import select
from sqlalchemy.orm import Session

from workwise.messaging.publisher import publish_one
from workwise.messaging.worker import handle
from workwise.db.models import OutboxEvent
from workwise.core import tracing


@pytest.fixture
def spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "tracer", lambda: provider.get_tracer("test-workwise"))
    try:
        yield exporter
    finally:
        provider.shutdown()


def test_trace_survives_http_durable_outbox_and_message_delivery(spans, db_client, db_session, db_connection, monkeypatch):
    company = db_client.post("/companies", json={"name": "Traced company"}).json()
    response = db_client.post("/jobs", json={"title": "Private job title", "company_id": company["id"], "location": "Remote"})
    assert response.status_code == 201
    event = db_session.scalars(select(OutboxEvent)).one()
    identity = response.headers["X-Trace-ID"]
    assert event.trace_context["traceparent"].split("-")[1] == identity
    assert "Private job title" not in str(event.trace_context)
    channel = Mock()
    with Session(bind=db_connection, join_transaction_mode="create_savepoint", expire_on_commit=False) as session:
        assert publish_one(session, channel)
    message = channel.basic_publish.call_args.kwargs
    monkeypatch.setattr("workwise.messaging.worker.record_notification", Mock(return_value=True))
    handle(Mock(), channel, SimpleNamespace(delivery_tag=9), message["properties"], message["body"], object())
    relevant = {item.name: item for item in spans.get_finished_spans() if item.name in ("POST /jobs", "outbox.publish", "notifications.process")}
    assert set(relevant) == {"POST /jobs", "outbox.publish", "notifications.process"}
    assert {format(item.context.trace_id, "032x") for item in relevant.values()} == {identity}
    assert relevant["outbox.publish"].parent.span_id == relevant["POST /jobs"].context.span_id
    assert relevant["notifications.process"].parent.span_id == relevant["outbox.publish"].context.span_id


def test_tracing_does_not_export_exception_details(spans):
    with pytest.raises(RuntimeError):
        with tracing.span("safe.operation"):
            raise RuntimeError("private credentials and exception details")
    recorded, = spans.get_finished_spans()
    assert recorded.status.status_code.name == "ERROR"
    assert recorded.attributes["error.type"] == "RuntimeError"
    assert recorded.events == ()
    assert "private credentials" not in recorded.to_json()


def test_malformed_context_starts_fresh_and_does_not_propagate_baggage(spans):
    with tracing.span("root", carrier={"traceparent": "malformed", "baggage": "password=private", "Authorization": "private"}):
        carrier = tracing.capture_context()
        assert set(carrier) == {"traceparent"}
        assert tracing.trace_id()
    assert "private" not in spans.get_finished_spans()[0].to_json()


def test_http_trace_uses_route_templates_and_excludes_request_data(spans, db_client):
    response = db_client.get("/jobs/987654?password=private", headers={"baggage": "password=private"})
    assert response.status_code == 404
    finished = spans.get_finished_spans()
    current = next(item for item in finished if item.name == "GET /jobs/{job_id}")
    assert current.attributes["http.route"] == "/jobs/{job_id}"
    assert "987654" not in current.to_json()
    assert "private" not in current.to_json()


def test_remote_sampled_context_is_respected(spans, db_client):
    identity = "abcdefffabcdefffabcdefffabcdefff"
    parent = "fedcbafedcbaabcd"
    response = db_client.get("/health", headers={"traceparent": f"00-{identity}-{parent}-01"})
    assert response.headers["X-Trace-ID"] == identity
    current, = spans.get_finished_spans()
    assert current.parent.span_id == int(parent, 16)
