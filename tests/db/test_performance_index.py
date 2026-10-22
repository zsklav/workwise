from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from workwise.core.paths import ROOT_DIR
from scripts.performance.fixture import cleanup, seed
from scripts.performance.query_plan import profile


def test_order_index_migration_preserves_rows_and_round_trips(db_connection):
    config=Config(str(ROOT_DIR/"alembic.ini"))
    config.attributes["connection"]=db_connection
    command.downgrade(config,"0008")
    company=db_connection.scalar(text("INSERT INTO companies(name) VALUES ('Index legacy company') RETURNING id"))
    db_connection.execute(text("INSERT INTO jobs(title,company_id,location) VALUES ('Index legacy job',:company,'Remote')"),{"company":company})
    before=[dict(r) for r in db_connection.execute(text("SELECT * FROM jobs ORDER BY id")).mappings()]
    for revision, expected, absent in (("0009","ix_jobs_company_id_id","ix_jobs_company_id"),("0008","ix_jobs_company_id","ix_jobs_company_id_id")):
        if revision == "0009":
            command.upgrade(config,revision)
        else:
            command.downgrade(config,revision)
        indexes={i["name"]:i["column_names"] for i in inspect(db_connection).get_indexes("jobs",schema="public")}
        assert indexes[expected] == (["company_id","id"] if revision == "0009" else ["company_id"])
        assert absent not in indexes
        assert [dict(r) for r in db_connection.execute(text("SELECT * FROM jobs ORDER BY id")).mappings()] == before


def test_query_plan_fixture_uses_temporary_tables_only(db_connection):
    before=db_connection.scalar(text("SELECT count(*) FROM public.jobs"))
    report=profile(db_connection,rows=1000,companies=10,repeats=1)
    assert db_connection.scalar(text("SELECT count(*) FROM public.jobs")) == before
    for phase in report["phases"].values():
        assert phase["index_bytes"] > 0
        for query in phase["queries"].values():
            assert query["summary"]["rows_returned"] == 20
            assert query["summary"]["median_execution_ms"] >= 0


def test_http_fixture_refuses_existing_data_and_cleanup_is_scoped(db_connection):
    fixture=seed(db_connection,rows=1000,companies=10)
    assert db_connection.scalar(text("SELECT count(*) FROM jobs")) == 1000
    import pytest
    with pytest.raises(RuntimeError,match="existing data was preserved"):
        seed(db_connection,rows=1000,companies=10)
    other=db_connection.scalar(text("INSERT INTO companies(name) VALUES ('Preserve unrelated company') RETURNING id"))
    db_connection.execute(text("INSERT INTO jobs(title,company_id,location) VALUES ('Keep unrelated job',:company,'Remote')"),{"company":other})
    cleanup(db_connection,fixture)
    assert db_connection.scalar(text("SELECT count(*) FROM jobs")) == 1
    assert db_connection.scalar(text("SELECT count(*) FROM companies")) == 1
    assert db_connection.scalar(text("SELECT count(*) FROM users")) == 0
