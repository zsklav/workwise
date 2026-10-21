#!/bin/sh
set -eu

# Independent database, independently versioned initial schema. Runtime cannot modify DDL.
psql --set=ON_ERROR_STOP=1 --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" \
    --set=worker_password="$NOTIFICATION_DB_PASSWORD" <<'SQL'
CREATE ROLE notification_worker LOGIN PASSWORD :'worker_password';
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE TABLE notification_schema_version (
    version integer PRIMARY KEY CHECK (version = 1)
);
INSERT INTO notification_schema_version VALUES (1);
CREATE TABLE notification_receipts (
    event_id uuid PRIMARY KEY,
    payload jsonb NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);
GRANT CONNECT ON DATABASE notifications TO notification_worker;
GRANT USAGE ON SCHEMA public TO notification_worker;
GRANT SELECT, INSERT ON notification_receipts TO notification_worker;
GRANT SELECT ON notification_schema_version TO notification_worker;
SQL
