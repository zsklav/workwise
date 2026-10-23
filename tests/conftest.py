from collections.abc import Iterator

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, create_engine, func, inspect, select, text
from sqlalchemy.orm import Session

from workwise.db.session import database_url, get_db
from workwise.api.main import app
from workwise.db.models import AuthRateLimit, Base, Company, Job, User, OutboxEvent, RefreshToken
from workwise.core.security import create_access_token, password_hasher

from workwise.core.paths import ROOT_DIR


TEST_PASSWORD = "test password for WorkWise 2026"
TEST_PASSWORD_HASH = password_hasher.hash(TEST_PASSWORD)


@pytest.fixture(scope="session")
def test_engine() -> Iterator[Engine]:
    # Always select the dedicated test database, regardless of DB_NAME in .env.
    engine = create_engine(
        database_url.set(database="workwise_test"),
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5},
    )

    initialized = False
    original_rate_keys = set()
    try:
        with engine.begin() as connection:
            if connection.scalar(text("SELECT current_database()")) != "workwise_test":
                raise RuntimeError("Database tests must use workwise_test.")

            inspector = inspect(connection)
            has_managed_tables = any(
                inspector.has_table(name, schema="public")
                for name in ("jobs", "companies", "users", "outbox_events")
            )
            if has_managed_tables and not inspector.has_table("alembic_version", schema="public"):
                raise RuntimeError(
                    "Unversioned test schema: verify and baseline it before running tests."
                )

            config = Config(str(ROOT_DIR / "alembic.ini"))
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

            migration_context = MigrationContext.configure(connection, opts={
                "include_schemas": True,
                "compare_type": True,
                "compare_server_default": True,
            })
            if compare_metadata(migration_context, Base.metadata):
                raise RuntimeError("Test migrations do not match the ORM models.")

            for model in (Job, Company, User, OutboxEvent, RefreshToken):
                if connection.scalar(select(func.count()).select_from(model)) != 0:
                    raise RuntimeError(
                        f"workwise_test.{model.__tablename__} must start empty. "
                        "Existing rows were not deleted."
                    )
            original_rate_keys = set(connection.scalars(select(AuthRateLimit.bucket_hash)))

        initialized = True
        yield engine
    finally:
        if initialized:
            # Keep pre-existing benchmark/security windows; remove only keys created by this run.
            with engine.begin() as connection:
                connection.execute(AuthRateLimit.__table__.delete().where(AuthRateLimit.bucket_hash.not_in(original_rate_keys)))
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
def anonymous_client(db_connection: Connection) -> Iterator[TestClient]:
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


@pytest.fixture
def auth_user(db_session: Session) -> User:
    user = User(username="test_owner", password_hash=TEST_PASSWORD_HASH, role="member", is_active=True)
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture
def db_client(anonymous_client: TestClient, auth_user: User) -> TestClient:
    anonymous_client.headers["Authorization"] = f"Bearer {create_access_token(auth_user.id)}"
    return anonymous_client
