from datetime import datetime, timedelta, timezone
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from workwise.core.paths import ROOT_DIR
from workwise.operations.retention import cleanup_batch


def test_upgrade_preserves_existing_business_rows_and_round_trips(db_connection):
    config = Config(str(ROOT_DIR / "alembic.ini"))
    config.attributes["connection"] = db_connection
    command.downgrade(config, "0010")
    db_connection.execute(text("INSERT INTO users(username,password_hash) VALUES ('migration_applicant','preserve-this-hash')"))
    before = list(db_connection.execute(text("SELECT * FROM users ORDER BY id")))
    command.upgrade(config, "0011")
    assert list(db_connection.execute(text("SELECT * FROM users ORDER BY id"))) == before
    assert db_connection.scalar(text("SELECT count(*) FROM job_applications")) == 0
    assert db_connection.scalar(text("SELECT count(*) FROM idempotency_records")) == 0
    command.downgrade(config, "0010")
    assert list(db_connection.execute(text("SELECT * FROM users ORDER BY id"))) == before
    command.upgrade(config, "0011")


def test_idempotency_cleanup_preserves_live_keys_and_user_data(db_connection):
    user = db_connection.scalar(text("INSERT INTO users(username,password_hash) VALUES ('idempotent_retention','preserve') RETURNING id"))
    now = datetime.now(timezone.utc)
    for token, expiry in (("a", now - timedelta(days=60)), ("b", now + timedelta(hours=1))):
        db_connection.execute(text("""INSERT INTO idempotency_records(id,user_id,scope,key_hash,request_hash,response_body,expires_at)
            VALUES(:id,:user,'jobs.create',:hash,:hash,'{}',:expires)"""),
            {"id": uuid4(), "user": user, "hash": token * 64, "expires": expiry})
    assert cleanup_batch(db_connection, "idempotency", days=30)["selected"] == 1
    assert db_connection.scalar(text("SELECT count(*) FROM idempotency_records")) == 2
    assert cleanup_batch(db_connection, "idempotency", days=30, apply=True)["deleted"] == 1
    assert db_connection.scalar(text("SELECT count(*) FROM idempotency_records")) == 1
    assert db_connection.scalar(text("SELECT count(*) FROM users WHERE id=:id"), {"id": user}) == 1
