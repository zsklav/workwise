from sqlalchemy import text

from scripts.database.backup import SCHEMA_SQL


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
