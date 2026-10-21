from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from database import get_db
from main import app


def test_readiness_checks_the_migrated_database(anonymous_client):
    response = anonymous_client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_readiness_rejects_stale_revision(anonymous_client, db_session):
    db_session.execute(text("UPDATE public.alembic_version SET version_num = 'stale'"))
    db_session.commit()
    response = anonymous_client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"detail": "Database migrations not ready"}


def test_database_failure_does_not_leak_details_or_break_liveness(anonymous_client):
    class UnavailableSession:
        def scalars(self, statement):
            raise OperationalError("SELECT version_num", {}, Exception("private connection details"))

    previous = app.dependency_overrides[get_db]
    app.dependency_overrides[get_db] = lambda: UnavailableSession()
    try:
        response = anonymous_client.get("/ready")
        assert response.status_code == 503
        assert response.json() == {"detail": "Database not ready"}
        assert "private connection details" not in response.text
        assert anonymous_client.get("/health").json() == {"status": "OK"}
    finally:
        app.dependency_overrides[get_db] = previous
