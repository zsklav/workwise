from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pika
import pytest
from sqlalchemy import delete, event as sqlalchemy_event, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from workwise.messaging.broker import DLQ, QUEUE, MAX_RETRIES
from workwise.messaging.events import job_created
from workwise.notifications.store import EventConflict, record_notification
from workwise.messaging.publisher import publish_one
from workwise.messaging.worker import handle
from workwise.db.models import Job, OutboxEvent


def create_job(client):
    company = client.post("/companies", json={"name": "Outbox company"}).json()
    response = client.post("/jobs", json={"title": "Outbox job", "company_id": company["id"], "location": "Remote"})
    assert response.status_code == 201
    return response.json()


def test_job_and_event_committed_together_and_survive_job_delete(db_client, db_session, auth_user):
    job = create_job(db_client)
    stored = db_session.scalars(select(OutboxEvent)).one()
    assert stored.payload["job_id"] == job["id"]
    assert stored.payload["company_id"] == job["company_id"]
    assert stored.payload["event_id"] == str(stored.id)
    assert stored.payload["event_type"] == "job.created.v2"
    assert stored.payload["recipient_id"] == auth_user.id
    assert stored.published_at is None
    assert db_client.delete(f"/jobs/{job['id']}").status_code == 204
    assert db_session.get(OutboxEvent, stored.id) is not None


def test_outbox_failure_rolls_back_job(db_client, db_session):
    def fail_outbox(session, *args):
        if any(isinstance(item, OutboxEvent) for item in session.new):
            raise RuntimeError("Simulated outbox storage failure")

    sqlalchemy_event.listen(Session, "before_flush", fail_outbox)
    try:
        company = db_client.post("/companies", json={"name": "Atomic failure"}).json()
        response = db_client.post("/jobs", json={"title": "Must roll back", "company_id": company["id"], "location": "Remote"})
        assert response.status_code == 500
    finally:
        sqlalchemy_event.remove(Session, "before_flush", fail_outbox)
    assert db_session.scalar(select(func.count()).select_from(Job)) == 0
    assert db_session.scalar(select(func.count()).select_from(OutboxEvent)) == 0


@pytest.mark.parametrize("confirmation_fails", [False, True])
def test_publisher_marks_only_confirmed_events(db_connection, db_session, confirmation_fails):
    envelope = job_created(7, 8)
    db_session.add(OutboxEvent(id=envelope.event_id, payload=envelope.model_dump(mode="json"), created_at=envelope.occurred_at))
    db_session.commit()
    channel = Mock()
    if confirmation_fails:
        channel.basic_publish.side_effect = pika.exceptions.AMQPConnectionError()
    with Session(bind=db_connection, join_transaction_mode="create_savepoint", expire_on_commit=False) as session:
        if confirmation_fails:
            with pytest.raises(pika.exceptions.AMQPConnectionError):
                publish_one(session, channel)
        else:
            assert publish_one(session, channel)
            assert not publish_one(session, channel)
    db_session.expire_all()
    stored = db_session.get(OutboxEvent, envelope.event_id)
    assert (stored.published_at is None) == confirmation_fails
    assert channel.basic_publish.call_args.kwargs["mandatory"] is True


def test_confirm_then_commit_failure_leaves_event_available_for_republication(db_connection, db_session):
    envelope = job_created(7, 8)
    db_session.add(OutboxEvent(id=envelope.event_id, payload=envelope.model_dump(mode="json"), created_at=envelope.occurred_at))
    db_session.commit()
    channel = Mock()
    with Session(bind=db_connection, join_transaction_mode="create_savepoint", expire_on_commit=False) as session:
        def fail_commit(*args):
            raise RuntimeError("Crash after broker confirmation")
        sqlalchemy_event.listen(session, "before_commit", fail_commit)
        with pytest.raises(RuntimeError, match="Crash after"):
            publish_one(session, channel)
    with Session(bind=db_connection, join_transaction_mode="create_savepoint", expire_on_commit=False) as session:
        assert publish_one(session, channel)
    assert channel.basic_publish.call_count == 2
    ids = [call.kwargs["properties"].message_id for call in channel.basic_publish.call_args_list]
    assert ids == [str(envelope.event_id)] * 2


def test_concurrent_publisher_skips_locked_event(test_engine):
    envelope = job_created(7, 8)
    try:
        with Session(test_engine) as session, session.begin():
            session.add(OutboxEvent(id=envelope.event_id, payload=envelope.model_dump(mode="json"), created_at=envelope.occurred_at))
        with Session(test_engine) as locked, locked.begin():
            locked.scalar(select(OutboxEvent).where(OutboxEvent.id == envelope.event_id).with_for_update())
            with Session(test_engine, expire_on_commit=False) as other:
                channel = Mock()
                assert not publish_one(other, channel)
                channel.basic_publish.assert_not_called()
    finally:
        with test_engine.begin() as connection:
            connection.execute(delete(OutboxEvent).where(OutboxEvent.id == envelope.event_id))


class TransactionStore:
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def begin(self):
        with self.connection.begin_nested():
            yield self.connection


def test_notification_duplicate_is_one_record_and_conflict_rejected(db_connection):
    db_connection.execute(text("CREATE TEMP TABLE notification_receipts (event_id uuid PRIMARY KEY, payload jsonb NOT NULL) ON COMMIT DROP"))
    store = TransactionStore(db_connection)
    envelope = job_created(1, 2)
    assert record_notification(store, envelope)
    assert not record_notification(store, envelope)
    changed = envelope.model_copy(update={"job_id": 3})
    with pytest.raises(EventConflict):
        record_notification(store, changed)
    assert db_connection.scalar(text("SELECT count(*) FROM notification_receipts")) == 1


def delivery(retries=0, body=None):
    envelope = job_created(1, 2)
    return (Mock(), Mock(), SimpleNamespace(delivery_tag=42),
            SimpleNamespace(message_id=str(envelope.event_id), headers={"retries": retries}),
            body if body is not None else envelope.model_dump_json().encode(), object())


@pytest.mark.parametrize("retries, destination", [(0, QUEUE), (MAX_RETRIES, DLQ), ("bad", DLQ)])
def test_worker_retries_then_dead_letters(monkeypatch, retries, destination):
    args = delivery(retries)
    monkeypatch.setattr("workwise.messaging.worker.record_notification", Mock(side_effect=OperationalError("simulated", {}, Exception())))
    handle(*args)
    assert args[1].basic_publish.call_args.kwargs["routing_key"] == destination
    args[1].basic_ack.assert_called_once_with(42)


@pytest.mark.parametrize("payload", [b"not-json", b'{}'])
def test_invalid_message_dead_letters_without_database(monkeypatch, payload):
    store = Mock()
    monkeypatch.setattr("workwise.messaging.worker.record_notification", store)
    args = delivery(body=payload)
    handle(*args)
    store.assert_not_called()
    assert args[1].basic_publish.call_args.kwargs["routing_key"] == DLQ
    args[1].basic_ack.assert_called_once_with(42)


def test_failed_dead_letter_publish_does_not_ack_original():
    args = delivery(body=b"poison")
    args[1].basic_publish.side_effect = pika.exceptions.AMQPConnectionError()
    with pytest.raises(pika.exceptions.AMQPConnectionError):
        handle(*args)
    args[1].basic_ack.assert_not_called()


def test_receipt_commits_before_ack_and_duplicate_acknowledged(monkeypatch):
    calls = []
    monkeypatch.setattr("workwise.messaging.worker.record_notification", lambda *args: calls.append("committed") or False)
    args = delivery()
    args[1].basic_ack.side_effect = lambda *args: calls.append("ack")
    handle(*args)
    assert calls == ["committed", "ack"]
    args[1].basic_publish.assert_not_called()
