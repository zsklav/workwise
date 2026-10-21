from collections.abc import Iterator
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from workwise.db.session import get_db
from workwise.api.main import app
from workwise.db.models import Company, Job, User, OutboxEvent


def test_crud_commits_visible_to_independent_connections(test_engine: Engine):
    # This case uses real commits in workwise_test instead of rollback fixtures.
    marker = f"Persistence check {uuid4()}"
    username = f"persist_{uuid4().hex}"
    job_id = None
    payload = {
        "title": marker,
        "location": "Remote",
        "description": "Initial persistent description",
    }

    def override_get_db() -> Iterator[Session]:
        with Session(test_engine) as session:
            yield session

    previous_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = override_get_db

    try:
        with TestClient(app) as client:
            signup = client.post("/auth/signup", json={
                "username": username, "password": "Persistence test password 2026!",
            })
            assert signup.status_code == 201
            login = client.post("/auth/login", data={
                "grant_type": "password", "username": username,
                "password": "Persistence test password 2026!",
            })
            assert login.status_code == 200
            client.headers["Authorization"] = "Bearer " + login.json()["access_token"]
            company = client.post("/companies", json={"name": marker})
            assert company.status_code == 201
            payload["company_id"] = company.json()["id"]
            response = client.post("/jobs", json=payload)
            assert response.status_code == 201
            job_id = response.json()["id"]

            with test_engine.connect() as connection:
                stored = connection.execute(
                    select(Job.__table__).where(Job.id == job_id)
                ).mappings().one()
                assert dict(stored) == {"id": job_id, **payload}

            updated_payload = {
                **payload,
                "location": "Bengaluru",
                "description": "Updated persistent description",
            }
            response = client.put(f"/jobs/{job_id}", json=updated_payload)
            assert response.status_code == 200

            with test_engine.connect() as connection:
                stored = connection.execute(
                    select(Job.__table__).where(Job.id == job_id)
                ).mappings().one()
                assert dict(stored) == {"id": job_id, **updated_payload}

            response = client.patch(f"/jobs/{job_id}", json={"description": None})
            assert response.status_code == 200

            with test_engine.connect() as connection:
                stored = connection.execute(
                    select(Job.__table__).where(Job.id == job_id)
                ).mappings().one()
                assert dict(stored) == {
                    "id": job_id, **updated_payload, "description": None
                }

            response = client.delete(f"/jobs/{job_id}")
            assert response.status_code == 204
            assert response.content == b""

            with test_engine.connect() as connection:
                assert connection.scalar(
                    select(Job.id).where(Job.id == job_id)
                ) is None
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)
        # Remove only this test's uniquely marked row, even if an assertion failed.
        with test_engine.begin() as connection:
            if job_id is not None:
                connection.execute(delete(OutboxEvent).where(OutboxEvent.payload["job_id"].as_integer() == job_id))
            connection.execute(delete(Job).where(Job.title == marker))
            connection.execute(delete(Company).where(Company.name == marker))
            connection.execute(delete(User).where(User.username == username))


def test_company_rename_and_delete_commits_visible_to_independent_connections(test_engine: Engine):
    marker = f"Company persistence {uuid4()}"
    username = f"persist_{uuid4().hex}"
    renamed = f"{marker} renamed"
    job_id = None

    def override_get_db() -> Iterator[Session]:
        with Session(test_engine) as session:
            yield session

    previous_overrides = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            signup = client.post("/auth/signup", json={
                "username": username, "password": "Persistence test password 2026!",
            })
            assert signup.status_code == 201
            login = client.post("/auth/login", data={
                "grant_type": "password", "username": username,
                "password": "Persistence test password 2026!",
            })
            assert login.status_code == 200
            client.headers["Authorization"] = "Bearer " + login.json()["access_token"]
            company = client.post("/companies", json={"name": marker})
            assert company.status_code == 201
            company_id = company.json()["id"]
            job = client.post("/jobs", json={
                "title": marker, "company_id": company_id, "location": "Remote",
            })
            assert job.status_code == 201
            job_id = job.json()["id"]
            path = f"/companies/{company_id}"

            updated = client.put(path, json={"name": renamed, "description": "Committed update"})
            assert updated.status_code == 200
            with test_engine.connect() as connection:
                assert connection.scalar(select(Company.name).where(Company.id == company_id)) == renamed
            assert client.get(f"/jobs/{job.json()['id']}").json()["company"] == renamed

            cleared = client.patch(path, json={"description": None})
            assert cleared.status_code == 200
            with test_engine.connect() as connection:
                stored = connection.execute(select(Company.__table__).where(Company.id == company_id)).mappings().one()
                assert dict(stored) == {"id": company_id, "name": renamed, "description": None, "owner_id": signup.json()["id"]}

            assert client.delete(path).status_code == 409
            with test_engine.connect() as connection:
                assert connection.scalar(select(Job.company_id).where(Job.id == job.json()["id"])) == company_id
                assert connection.scalar(select(Company.name).where(Company.id == company_id)) == renamed

            assert client.delete(f"/jobs/{job.json()['id']}").status_code == 204
            assert client.delete(path).status_code == 204
            with test_engine.connect() as connection:
                assert connection.scalar(select(Company.id).where(Company.id == company_id)) is None
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)
        with test_engine.begin() as connection:
            if job_id is not None:
                connection.execute(delete(OutboxEvent).where(OutboxEvent.payload["job_id"].as_integer() == job_id))
            connection.execute(delete(Job).where(Job.title == marker))
            connection.execute(delete(Company).where(Company.name.in_([marker, renamed])))
            connection.execute(delete(User).where(User.username == username))
