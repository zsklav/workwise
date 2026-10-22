from contextlib import contextmanager
from types import SimpleNamespace

from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from workwise.api.main import app
from workwise.api.routes import operations
from workwise.notifications import api as inbox_api
from workwise.operations import metrics
from scripts.docker.monitoring_secrets import write_token

TOKEN = "isolated-metrics-test-token-with-enough-characters"


@pytest.mark.parametrize("application", [app, inbox_api.app])
def test_service_metrics_require_separate_credentials(monkeypatch, application):
    monkeypatch.setattr(metrics, "settings", lambda: SimpleNamespace(metrics_token=SecretStr(TOKEN)))
    with TestClient(application) as client:
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic wrong"}):
            response = client.get("/internal/metrics", headers=headers)
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Bearer"
        monkeypatch.setattr(metrics, "settings", lambda: SimpleNamespace(metrics_token=None))
        assert client.get("/internal/metrics", headers={"Authorization": "Bearer " + TOKEN}).status_code == 404


@pytest.mark.parametrize("value", ["short", "replace_with_a_placeholder_long_enough_for_validation", "has spaces " * 4, "é" * 40])
def test_invalid_scrape_secrets_are_rejected(value):
    with pytest.raises(ValidationError):
        metrics.MonitoringSettings(_env_file=None, metrics_token=value)


def test_outbox_aggregates_are_live_bounded_and_do_not_expose_payloads(db_connection):
    db_connection.execute(text("""INSERT INTO outbox_events(id,payload,created_at,published_at) VALUES
        (gen_random_uuid(), '{"private":"must-not-export"}', now()-interval '5 minutes', NULL),
        (gen_random_uuid(), '{}', now(), NULL),
        (gen_random_uuid(), '{}', now()-interval '2 days', now()-interval '1 day')"""))
    @contextmanager
    def begin():
        with db_connection.begin_nested():
            yield db_connection
    result = metrics.database_metrics(SimpleNamespace(begin=begin), "outbox").decode()
    assert "workwise_database_up 1.0" in result
    assert "workwise_outbox_pending 2.0" in result
    assert "workwise_outbox_published 1.0" in result
    assert "workwise_outbox_oldest_seconds 300.0" in result
    assert "must-not-export" not in result


def test_database_failure_keeps_http_metrics_available(monkeypatch):
    @contextmanager
    def broken():
        raise OperationalError("private query", {}, Exception("private database password"))
        yield
    monkeypatch.setattr(metrics, "settings", lambda: SimpleNamespace(metrics_token=SecretStr(TOKEN)))
    monkeypatch.setattr(operations, "monitoring_database", lambda: SimpleNamespace(begin=broken))
    with TestClient(app) as client:
        client.get("/health")
        response = client.get("/internal/metrics", headers={"Authorization": "Bearer " + TOKEN})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "workwise_http_requests_total" in response.text
    assert "workwise_database_up 0.0" in response.text
    assert "workwise_outbox_pending" not in response.text
    assert all(value not in response.text for value in (TOKEN, "private query", "private database password"))


def test_token_volume_is_initialized_without_logging_or_overwriting_other_files(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("METRICS_TOKEN", TOKEN)
    (tmp_path / "keep").write_text("preserve")
    write_token(tmp_path)
    assert (tmp_path / "metrics-token").read_text() == TOKEN
    replacement = TOKEN + "-rotated"
    monkeypatch.setenv("METRICS_TOKEN", replacement)
    write_token(tmp_path)
    assert (tmp_path / "metrics-token").read_text() == replacement
    assert (tmp_path / "keep").read_text() == "preserve"
    assert TOKEN not in capsys.readouterr().out
