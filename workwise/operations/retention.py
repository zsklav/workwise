"""Bounded maintenance batches; preview by default, --apply explicitly deletes eligible rows."""
import argparse
from datetime import datetime, timedelta, timezone
import json

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL


TARGETS = {
    "outbox": ("outbox_events", "id", "published_at IS NOT NULL AND published_at < :cutoff", "published_at, id"),
    "inbox": ("notifications", "event_id", "read_at IS NOT NULL AND read_at < :cutoff AND created_at < :cutoff", "read_at, event_id"),
}


def cleanup_batch(connection, target, *, days, batch_size=500, apply=False, now=None):
    if target not in TARGETS or not 7 <= days <= 3650 or not 1 <= batch_size <= 1000:
        raise ValueError("Target, days (7..3650), or batch size (1..1000) is invalid")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Retention time must be timezone aware")
    cutoff = now - timedelta(days=days)
    table, key, condition, order = TARGETS[target]
    params = {"cutoff": cutoff, "batch": batch_size}
    connection.execute(text("SET LOCAL statement_timeout = '5s'"))
    connection.execute(text("SET LOCAL lock_timeout = '1s'"))
    if apply:
        # Claims and deletion share one transaction; other cleaners skip claimed rows.
        statement = f"""WITH candidates AS (
            SELECT {key} FROM {table} WHERE {condition} ORDER BY {order}
            LIMIT :batch FOR UPDATE SKIP LOCKED
        ) DELETE FROM {table} AS destination USING candidates
          WHERE destination.{key} = candidates.{key} RETURNING destination.{key}"""
    else:
        statement = f"SELECT {key} FROM {table} WHERE {condition} ORDER BY {order} LIMIT :batch"
    count = len(connection.execute(text(statement), params).all())
    return {"target": target, "mode": "apply" if apply else "preview", "cutoff": cutoff.isoformat(),
            "batch_size": batch_size, "selected": count, "deleted": count if apply else 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=TARGETS)
    parser.add_argument("--days", type=int, help="Minimum age; defaults to 30 for outbox, 90 for read inbox")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    days = args.days if args.days is not None else (30 if args.target == "outbox" else 90)
    if not 7 <= days <= 3650 or not 1 <= args.batch_size <= 1000:
        parser.error("--days must be 7..3650 and --batch-size must be 1..1000")
    if args.target == "outbox":
        from workwise.core.config import MigrationSettings
        s = MigrationSettings()
        url = URL.create("postgresql+psycopg", host=s.db_host, port=s.db_port, database=s.db_name,
                         username=s.migration_db_user, password=s.migration_db_password.get_secret_value())
    else:
        from scripts.database.migrate_notifications import Settings
        s = Settings()
        url = URL.create("postgresql+psycopg", host=s.notification_db_host, database="notifications",
                         username="postgres", password=s.notification_admin_password.get_secret_value())
    database = create_engine(url, connect_args={"connect_timeout": 5})
    try:
        with database.begin() as connection:
            report = cleanup_batch(connection, args.target, days=days, batch_size=args.batch_size, apply=args.apply)
        print(json.dumps({"event": "retention_batch", **report}), flush=True)
    finally:
        database.dispose()


if __name__ == "__main__":
    main()
