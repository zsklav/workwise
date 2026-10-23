from alembic import command
from alembic.config import Config
from sqlalchemy import text

from workwise.core.paths import ROOT_DIR
from workwise.operations.retention import cleanup_batch


def test_auth_migration_preserves_legacy_user_data_and_round_trips(db_connection):
    config=Config(str(ROOT_DIR/"alembic.ini"))
    config.attributes["connection"]=db_connection
    command.downgrade(config,"0009")
    db_connection.execute(text("INSERT INTO users(username,password_hash,role,is_active) VALUES ('legacy_security','preserve-hash','admin',false)"))
    before=[dict(r) for r in db_connection.execute(text("SELECT * FROM users ORDER BY id")).mappings()]
    command.upgrade(config,"0010")
    after=[dict(r) for r in db_connection.execute(text("SELECT * FROM users ORDER BY id")).mappings()]
    assert all(r.pop("token_version")==1 for r in after)
    assert after==before
    command.downgrade(config,"0009")
    assert [dict(r) for r in db_connection.execute(text("SELECT * FROM users ORDER BY id")).mappings()]==before


def test_auth_retention_preserves_current_and_recently_expired_state(db_connection):
    user=db_connection.scalar(text("INSERT INTO users(username,password_hash) VALUES ('retention_security','hash') RETURNING id"))
    db_connection.execute(text("""INSERT INTO auth_refresh_tokens(id,user_id,token_hash,token_version,expires_at) VALUES
        (gen_random_uuid(),:user,repeat('a',64),1,now()-interval '60 days'),
        (gen_random_uuid(),:user,repeat('b',64),1,now()+interval '7 days')"""),{"user":user})
    db_connection.execute(text("""INSERT INTO auth_rate_limits(bucket_hash,attempts,window_end) VALUES
        (repeat('a',64),1,now()-interval '60 days'),(repeat('b',64),1,now()-interval '1 day')"""))
    for target in ("auth-tokens","auth-limits"):
        assert cleanup_batch(db_connection,target,days=30)["selected"]==1
        assert cleanup_batch(db_connection,target,days=30,apply=True)["deleted"]==1
    assert db_connection.scalar(text("SELECT count(*) FROM auth_refresh_tokens"))==1
    assert db_connection.scalar(text("SELECT count(*) FROM auth_rate_limits WHERE bucket_hash IN (repeat('a',64),repeat('b',64))"))==1
