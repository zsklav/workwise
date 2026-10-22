from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from workwise.messaging.events import job_created
from workwise.notifications.migration import upgrade_schema
from workwise.notifications.store import record_notification
from workwise.operations.metrics import database_metrics
from workwise.operations.retention import cleanup_batch


def seed_outbox(connection):
    ids = [uuid4() for _ in range(5)]
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=100)
    for identity, published in zip(ids, (old, old, None, now, now - timedelta(days=30))):
        connection.execute(text("INSERT INTO outbox_events(id,payload,created_at,published_at) VALUES (:id,'{}',:old,:published)"),
                           {"id": identity, "old": old, "published": published})
    return ids, now


def test_outbox_preview_apply_boundary_and_batch_limit(db_connection):
    ids, now = seed_outbox(db_connection)
    preview = cleanup_batch(db_connection, "outbox", days=30, batch_size=1, now=now)
    assert preview["selected"] == 1 and preview["deleted"] == 0
    assert db_connection.scalar(text("SELECT count(*) FROM outbox_events")) == 5
    for _ in range(2):
        report = cleanup_batch(db_connection, "outbox", days=30, batch_size=1, apply=True, now=now)
        assert report["deleted"] == 1
    assert cleanup_batch(db_connection, "outbox", days=30, apply=True, now=now)["deleted"] == 0
    remaining = set(db_connection.scalars(text("SELECT id FROM outbox_events")))
    assert remaining == set(ids[2:])  # Pending, recent, and exactly-at-cutoff rows survive.


def test_cleanup_deletion_rolls_back_with_transaction(db_connection):
    ids, now = seed_outbox(db_connection)
    transaction = db_connection.begin_nested()
    assert cleanup_batch(db_connection, "outbox", days=30, apply=True, now=now)["deleted"] == 2
    transaction.rollback()
    assert set(db_connection.scalars(text("SELECT id FROM outbox_events"))) == set(ids)


@pytest.mark.parametrize("target,days,batch", [("unknown",30,500),("outbox",0,500),("outbox",6,500),("inbox",3651,500),("outbox",30,0),("outbox",30,1001)])
def test_unsafe_policies_fail_before_database_access(target, days, batch):
    with pytest.raises(ValueError):
        cleanup_batch(None, target, days=days, batch_size=batch, apply=True)


def test_read_inbox_cleanup_preserves_unread_and_deduplication(db_connection):
    db_connection.execute(text("CREATE TABLE notification_schema_version(version integer PRIMARY KEY CHECK (version = 1))"))
    db_connection.execute(text("INSERT INTO notification_schema_version VALUES (1)"))
    db_connection.execute(text("CREATE TABLE notification_receipts(event_id uuid PRIMARY KEY, payload jsonb NOT NULL, recorded_at timestamptz NOT NULL DEFAULT now())"))
    upgrade_schema(db_connection)
    @contextmanager
    def begin():
        with db_connection.begin_nested():
            yield db_connection
    store = SimpleNamespace(begin=begin)
    events = [job_created(11, 12, 13) for _ in range(4)]
    for event in events:
        assert record_notification(store, event)
    db_connection.execute(text("UPDATE notifications SET created_at=now()-interval '150 days', read_at=now()-interval '100 days' WHERE event_id=:id"), {"id": events[0].event_id})
    # An old unread row, a recent read, and an old row read recently all survive.
    db_connection.execute(text("UPDATE notifications SET created_at=now()-interval '150 days' WHERE event_id IN (:a,:b)"), {"a":events[1].event_id,"b":events[3].event_id})
    db_connection.execute(text("UPDATE notifications SET read_at=now() WHERE event_id IN (:a,:b)"), {"a":events[2].event_id,"b":events[3].event_id})
    assert cleanup_batch(db_connection, "inbox", days=90)["selected"] == 1
    assert cleanup_batch(db_connection, "inbox", days=90, apply=True)["deleted"] == 1
    assert db_connection.scalar(text("SELECT count(*) FROM notification_receipts")) == 4
    assert db_connection.scalar(text("SELECT count(*) FROM notifications")) == 3
    assert not record_notification(store, events[0])
    assert db_connection.scalar(text("SELECT count(*) FROM notifications WHERE event_id=:id"), {"id":events[0].event_id}) == 0
    metrics = database_metrics(store, "inbox").decode()
    assert "workwise_inbox_unread 1.0" in metrics
    assert "workwise_inbox_read 2.0" in metrics


def test_concurrent_cleaners_skip_locked_rows(test_engine):
    ids = []
    try:
        with test_engine.begin() as connection:
            ids, now = seed_outbox(connection)
        with test_engine.connect() as first, test_engine.connect() as second:
            a, b = first.begin(), second.begin()
            assert cleanup_batch(first, "outbox", days=30, batch_size=1, apply=True, now=now)["deleted"] == 1
            assert cleanup_batch(second, "outbox", days=30, batch_size=1, apply=True, now=now)["deleted"] == 1
            a.commit()
            b.commit()
        with test_engine.connect() as check:
            assert set(check.scalars(text("SELECT id FROM outbox_events"))) == set(ids[2:])
    finally:
        if ids:
            with test_engine.begin() as connection:
                for identity in ids:
                    connection.execute(text("DELETE FROM outbox_events WHERE id=:id"), {"id":identity})
