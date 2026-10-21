from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import jwt
from pydantic import SecretStr, ValidationError
import pytest
from sqlalchemy import event as sql_event, text
from sqlalchemy.exc import OperationalError

from main import app as public_app
from messaging.events import job_created, parse_event
from messaging.notification_store import EventConflict, record_notification
from models import User
from notifications import api
from notifications.contracts import service_token
from notifications.migration import upgrade_schema
from routers.notifications import get_gateway
from security import create_access_token

SECRET = "isolated-notification-test-signing-key-with-enough-letters"


class Store:
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def begin(self):
        with self.connection.begin_nested():
            yield self.connection


@pytest.fixture
def notification_state(db_connection):
    db_connection.execute(text("CREATE TABLE notification_schema_version (version integer PRIMARY KEY CHECK (version = 1))"))
    db_connection.execute(text("INSERT INTO notification_schema_version VALUES (1)"))
    db_connection.execute(text("CREATE TABLE notification_receipts (event_id uuid PRIMARY KEY, payload jsonb NOT NULL, recorded_at timestamptz NOT NULL DEFAULT now())"))
    upgrade_schema(db_connection)
    return Store(db_connection)


@pytest.fixture
def inbox(notification_state, db_connection, monkeypatch):
    monkeypatch.setattr(api, "settings", lambda: SimpleNamespace(notification_service_secret=SecretStr(SECRET)))

    def connection():
        with db_connection.begin_nested():
            yield db_connection

    previous = api.app.dependency_overrides.copy()
    api.app.dependency_overrides[api.get_connection] = connection
    try:
        with TestClient(api.app) as client:
            client.headers["Authorization"] = "Bearer " + service_token(7, SECRET)
            yield client
    finally:
        api.app.dependency_overrides.clear()
        api.app.dependency_overrides.update(previous)


@pytest.fixture
def gateway(inbox):
    def transport(request):
        result = inbox.request(request.method, request.url.raw_path.decode(), headers=dict(request.headers))
        return httpx.Response(result.status_code, content=result.content, headers=dict(result.headers))

    with httpx.Client(transport=httpx.MockTransport(transport), base_url="http://notifications") as client:
        def dependency():
            yield client, SECRET
        previous = public_app.dependency_overrides.copy()
        public_app.dependency_overrides[get_gateway] = dependency
        try:
            yield
        finally:
            public_app.dependency_overrides.clear()
            public_app.dependency_overrides.update(previous)


def test_v2_delivery_creates_inbox_once_legacy_is_receipt_only(notification_state, db_connection, inbox):
    legacy = job_created(1, 2)
    envelope = job_created(3, 4, 7)
    assert record_notification(notification_state, legacy)
    assert record_notification(notification_state, envelope)
    assert not record_notification(notification_state, envelope)
    assert db_connection.scalar(text("SELECT count(*) FROM notification_receipts")) == 2
    rows = inbox.get("/notifications").json()
    assert len(rows) == 1
    assert rows[0]["event_id"] == str(envelope.event_id)
    assert rows[0]["job_id"] == 3
    assert rows[0]["read_at"] is None
    assert "recipient_id" not in rows[0]


def test_receipt_rolls_back_if_inbox_insert_fails(notification_state, db_connection):
    def fail(connection, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO notifications "):
            raise OperationalError("simulated", {}, Exception())
    sql_event.listen(db_connection, "before_cursor_execute", fail)
    try:
        with pytest.raises(OperationalError):
            record_notification(notification_state, job_created(1, 2, 7))
    finally:
        sql_event.remove(db_connection, "before_cursor_execute", fail)
    assert db_connection.scalar(text("SELECT count(*) FROM notification_receipts")) == 0
    assert db_connection.scalar(text("SELECT count(*) FROM notifications")) == 0


def test_notification_migration_preserves_receipts_and_is_repeatable(db_connection):
    db_connection.execute(text("CREATE TABLE notification_schema_version (version integer PRIMARY KEY CHECK (version = 1))"))
    db_connection.execute(text("INSERT INTO notification_schema_version VALUES (1)"))
    db_connection.execute(text("CREATE TABLE notification_receipts (event_id uuid PRIMARY KEY, payload jsonb NOT NULL)"))
    envelope = job_created(9, 10)
    record_notification(Store(db_connection), envelope)
    before = list(db_connection.execute(text("SELECT * FROM notification_receipts")))
    upgrade_schema(db_connection)
    upgrade_schema(db_connection)
    assert list(db_connection.execute(text("SELECT * FROM notification_receipts"))) == before
    assert db_connection.scalar(text("SELECT count(*) FROM notifications")) == 0
    assert db_connection.scalar(text("SELECT version FROM notification_schema_version")) == 2


def test_read_is_recipient_scoped_idempotent_and_filterable(notification_state, inbox):
    envelope = job_created(1, 2, 7)
    other = job_created(3, 4, 8)
    for item in (envelope, other):
        record_notification(notification_state, item)
    assert len(inbox.get("/notifications?unread_only=true").json()) == 1
    assert inbox.patch(f"/notifications/{other.event_id}/read").status_code == 404
    result = inbox.patch(f"/notifications/{envelope.event_id}/read")
    assert result.status_code == 200
    assert result.headers["Cache-Control"] == "no-store"
    assert result.json()["read_at"] is not None
    assert inbox.patch(f"/notifications/{envelope.event_id}/read").json() == result.json()
    assert inbox.get("/notifications?unread_only=true").json() == []
    assert len(inbox.get("/notifications").json()) == 1
    assert inbox.patch(f"/notifications/{uuid4()}/read").status_code == 404


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "unread_only=notbool"])
def test_inbox_validates_pagination(inbox, query):
    assert inbox.get("/notifications?" + query).status_code == 422


@pytest.mark.parametrize("kind", ["missing", "wrong-secret", "expired", "access-token", "wrong-type", "long-lifetime"])
def test_internal_service_rejects_untrusted_tokens(inbox, kind):
    claims = jwt.decode(service_token(7, SECRET), SECRET, algorithms=["HS256"], audience="workwise-notifications")
    if kind == "expired":
        claims.update(iat=int(datetime.now(timezone.utc).timestamp()) - 60, exp=int(datetime.now(timezone.utc).timestamp()) - 30,
                      nbf=int(datetime.now(timezone.utc).timestamp()) - 60)
    if kind == "wrong-type":
        claims["type"] = "access"
    if kind == "long-lifetime":
        claims["exp"] = claims["iat"] + 3600
    token = jwt.encode(claims, "untrusted-signing-secret-with-enough-letters" if kind == "wrong-secret" else SECRET, algorithm="HS256")
    if kind == "access-token":
        token = create_access_token(7)
    if kind == "missing":
        inbox.headers.pop("Authorization")
        result = inbox.get("/notifications")
    else:
        result = inbox.get("/notifications", headers={"Authorization": "Bearer " + token})
    assert result.status_code == 401


def test_gateway_job_to_recipient_inbox(db_client, db_session, db_connection, auth_user, notification_state, gateway):
    company = db_client.post("/companies", json={"name": "Recipient company"}).json()
    job = db_client.post("/jobs", json={"title": "Recipient job", "company_id": company["id"], "location": "Remote"}).json()
    payload = db_connection.scalar(text("SELECT payload FROM outbox_events WHERE payload->>'job_id' = :id"), {"id": str(job["id"])})
    import json
    envelope = parse_event(json.dumps(payload).encode())
    assert envelope.recipient_id == auth_user.id
    record_notification(notification_state, envelope)
    result = db_client.get("/notifications")
    assert result.status_code == 200
    assert result.json()[0]["job_id"] == job["id"]
    assert db_client.patch(f"/notifications/{envelope.event_id}/read").status_code == 200
    other = User(username="another_inbox_user", password_hash=auth_user.password_hash)
    db_session.add(other)
    db_session.commit()
    db_client.headers["Authorization"] = "Bearer " + create_access_token(other.id)
    assert db_client.get(f"/notifications?recipient_id={auth_user.id}").json() == []
    assert db_client.patch(f"/notifications/{envelope.event_id}/read").status_code == 404


def test_gateway_requires_login_before_contacting_service(anonymous_client):
    assert anonymous_client.get("/notifications").status_code == 401
    assert anonymous_client.patch(f"/notifications/{uuid4()}/read").status_code == 401


def test_gateway_dependency_failure_is_503_without_details(db_client):
    def transport(request):
        raise httpx.ReadTimeout("sensitive upstream connection details")
    with httpx.Client(transport=httpx.MockTransport(transport), base_url="http://notifications") as client:
        public_app.dependency_overrides[get_gateway] = lambda: (client, SECRET)
        try:
            response = db_client.get("/notifications")
            assert response.status_code == 503
            assert response.json() == {"detail": "Notification service unavailable"}
        finally:
            public_app.dependency_overrides.pop(get_gateway)


def test_recipient_id_is_mandatory_in_v2():
    envelope = job_created(1, 2, 7).model_dump_json()
    import json
    payload = json.loads(envelope)
    del payload["recipient_id"]
    with pytest.raises(ValidationError):
        parse_event(json.dumps(payload).encode())


def test_commit_failure_returns_503_and_rolls_back_read_marker(notification_state, db_connection, inbox, monkeypatch):
    envelope = job_created(1, 2, 7)
    record_notification(notification_state, envelope)

    class FailingCommit:
        @contextmanager
        def begin(self):
            with db_connection.begin_nested():
                yield db_connection
                raise OperationalError("simulated commit failure", {}, Exception())

    monkeypatch.setattr(api, "database", lambda: FailingCommit())
    override = api.app.dependency_overrides.pop(api.get_connection)
    try:
        result = inbox.patch(f"/notifications/{envelope.event_id}/read")
        assert result.status_code == 503
        assert db_connection.scalar(text("SELECT read_at FROM notifications")) is None
    finally:
        api.app.dependency_overrides[api.get_connection] = override


@pytest.mark.parametrize("status, payload", [(200, {"bad": "shape"}), (200, [{}]), (502, {"detail": "private upstream error"})])
def test_gateway_rejects_bad_upstream_contracts(db_client, status, payload):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status, json=payload)),
                      base_url="http://notifications") as client:
        public_app.dependency_overrides[get_gateway] = lambda: (client, SECRET)
        try:
            result = db_client.get("/notifications")
            assert result.status_code == 503
            assert result.json() == {"detail": "Notification service unavailable"}
        finally:
            public_app.dependency_overrides.pop(get_gateway)
