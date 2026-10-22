from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from workwise.core.paths import ROOT_DIR


def test_version_migration_preserves_existing_fields_and_round_trips(db_connection):
    config = Config(str(ROOT_DIR / "alembic.ini"))
    config.attributes["connection"] = db_connection
    command.downgrade(config, "0007")
    company_id = db_connection.scalar(text("INSERT INTO companies(name, description) VALUES ('Legacy revision company', 'Preserve details') RETURNING id"))
    db_connection.execute(text("INSERT INTO jobs(title, company_id, location, description) VALUES ('Legacy revision job', :id, 'Remote', 'Preserve job')"), {"id": company_id})
    before = {table: [dict(row) for row in db_connection.execute(text(f"SELECT * FROM {table} ORDER BY id")).mappings()]
              for table in ("jobs", "companies")}
    command.upgrade(config, "0008")
    for table in before:
        after = [dict(row) for row in db_connection.execute(text(f"SELECT * FROM {table} ORDER BY id")).mappings()]
        assert all(row.pop("version") == 1 for row in after)
        assert after == before[table]
        with pytest.raises(IntegrityError):
            with db_connection.begin_nested():
                db_connection.execute(text(f"UPDATE {table} SET version = 0"))
    command.downgrade(config, "0007")
    for table in before:
        assert [dict(row) for row in db_connection.execute(text(f"SELECT * FROM {table} ORDER BY id")).mappings()] == before[table]
