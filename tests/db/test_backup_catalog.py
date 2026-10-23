import json
from unittest.mock import Mock

from sqlalchemy import text

from scripts.database.backup import SCHEMA_SQL, check_signature


def test_backup_schema_comparison_preserves_column_order_without_dropped_column_slots(db_connection):
    # A logical restore removes physical attnum gaps left by historical migrations.
    db_connection.execute(text("CREATE TEMP TABLE ww_backup_shape (id integer, obsolete text, title text)"))
    db_connection.execute(text("ALTER TABLE ww_backup_shape DROP COLUMN obsolete"))
    schema = db_connection.scalar(text("SELECT nspname FROM pg_namespace WHERE oid=pg_my_temp_schema()"))
    query = text(SCHEMA_SQL.replace("table_schema='public'", "table_schema='" + schema + "'"))
    before = db_connection.scalar(query)["columns"]
    db_connection.execute(text("DROP TABLE ww_backup_shape"))
    db_connection.execute(text("CREATE TEMP TABLE ww_backup_shape (id integer, title text)"))
    after = db_connection.scalar(query)["columns"]
    assert before == after
    assert [column["column_name"] for column in after] == ["id", "title"]
    assert [column["ordinal_position"] for column in after] == [1, 2]


def test_backup_check_comparison_normalizes_array_casts_and_detects_changed_rules(db_connection):
    # This is the real users-role CHECK before/after PostgreSQL's dump/reparse.
    snapshot = Mock()
    snapshot.query.side_effect = lambda statement: [json.dumps(db_connection.scalar(text(statement))).encode()]
    original = "role::text = ANY ((ARRAY['member'::varchar,'admin'::varchar])::text[])"
    restored = "role::text = ANY (ARRAY[('member'::varchar)::text,('admin'::varchar)::text])"
    changed = "role::text = ANY (ARRAY[('member'::varchar)::text,('superadmin'::varchar)::text])"
    assert check_signature(snapshot, "users", original) == check_signature(snapshot, "users", restored)
    assert check_signature(snapshot, "users", original) != check_signature(snapshot, "users", changed)
