from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, create_engine, func, inspect, select, text
from sqlalchemy.orm import Session

from database import database_url, get_db
from main import app
from models import Base, Company, Job


@pytest.fixture(scope="session")
def test_engine() -> Iterator[Engine]:
    # Always select the dedicated test database, regardless of DB_NAME in .env.
    engine = create_engine(
        database_url.set(database="workwise_test"),
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5},
    )

    try:
        with engine.begin() as connection:
            if connection.scalar(text("SELECT current_database()")) != "workwise_test":
                raise RuntimeError("Database tests must use workwise_test.")

            inspector = inspect(connection)
            has_managed_tables = any(
                inspector.has_table(name, schema="public")
                for name in ("jobs", "companies")
            )
            if has_managed_tables and not inspector.has_table("alembic_version", schema="public"):
                raise RuntimeError(
                    "Unversioned test schema: verify and baseline it before running tests."
                )

            config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

            migration_context = MigrationContext.configure(connection, opts={
                "include_schemas": True,
                "compare_type": True,
                "compare_server_default": True,
            })
            if compare_metadata(migration_context, Base.metadata):
                raise RuntimeError("Test migrations do not match the ORM models.")

            for model in (Job, Company):
                if connection.scalar(select(func.count()).select_from(model)) != 0:
                    raise RuntimeError(
                        f"workwise_test.{model.__tablename__} must start empty. "
                        "Existing rows were not deleted."
                    )

        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db_connection(test_engine: Engine) -> Iterator[Connection]:
    with test_engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


@pytest.fixture
def db_session(db_connection: Connection) -> Iterator[Session]:
    with Session(
        bind=db_connection,
        join_transaction_mode="create_savepoint",
    ) as session:
        yield session


@pytest.fixture
def db_client(db_connection: Connection) -> Iterator[TestClient]:
    def override_get_db() -> Iterator[Session]:
        # Each request gets its own session inside this test's transaction.
        with Session(
            bind=db_connection,
            join_transaction_mode="create_savepoint",
        ) as session:
            yield session

    previous_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)
