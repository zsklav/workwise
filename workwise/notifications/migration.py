"""Notification schema v1 -> v2, run only with this database's operator credentials."""
from sqlalchemy import text


def upgrade_schema(connection):
    connection.execute(text("SET LOCAL lock_timeout = '5s'"))
    connection.execute(text("SELECT pg_advisory_xact_lock(9147302)"))
    versions = tuple(connection.scalars(text("SELECT version FROM notification_schema_version FOR UPDATE")))
    if versions == (2,):
        return
    if versions != (1,):
        raise RuntimeError("Unsupported notification schema version")
    connection.execute(text("""
        CREATE TABLE notifications (
            event_id uuid PRIMARY KEY REFERENCES notification_receipts(event_id) ON DELETE RESTRICT,
            recipient_id integer NOT NULL CHECK (recipient_id > 0),
            job_id integer NOT NULL,
            company_id integer NOT NULL,
            message text NOT NULL,
            occurred_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            read_at timestamptz
        )
    """))
    connection.execute(text("CREATE INDEX ix_notifications_recipient_created ON notifications (recipient_id, created_at DESC, event_id DESC)"))
    connection.execute(text("ALTER TABLE notification_schema_version DROP CONSTRAINT notification_schema_version_version_check"))
    connection.execute(text("UPDATE notification_schema_version SET version = 2"))
    connection.execute(text("ALTER TABLE notification_schema_version ADD CONSTRAINT notification_schema_version_version_check CHECK (version = 2)"))
