from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import delete, event, func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from workwise.api.concurrency import CONFLICT_DETAIL
from workwise.api.main import app
from workwise.core.security import create_access_token, password_hasher
from workwise.db.models import Company, Job, OutboxEvent, User
from workwise.db.session import get_db


@pytest.fixture(params=["jobs", "companies"])
def resource(db_client, request):
    company = db_client.post("/companies", json={"name": "Versioned company", "description": "Keep company details"}).json()
    if request.param == "companies":
        return "/companies", company, {"name": company["name"], "description": company["description"]}
    payload = {"title": "Versioned job", "company_id": company["id"], "location": "Remote", "description": "Keep job details"}
    job = db_client.post("/jobs", json=payload).json()
    return "/jobs", job, payload


@pytest.mark.parametrize("method", ["put", "patch"])
def test_stale_client_cannot_overwrite_and_can_refetch_then_retry(db_client, resource, method):
    collection, original, payload = resource
    path = f"{collection}/{original['id']}"
    assert original["version"] == 1
    field = "title" if collection == "/jobs" else "name"
    first = db_client.request(method, path, json={**payload, "version": original["version"], field: "First writer"})
    assert first.status_code == 200
    assert first.json()["version"] == 2
    stale = db_client.request(method, path, json={**payload, "version": original["version"], field: "Stale writer"})
    assert stale.status_code == 409
    assert stale.json() == {"detail": CONFLICT_DETAIL}
    latest = db_client.get(path).json()
    assert latest == first.json()
    retried = db_client.request(method, path, json={**payload, "version": latest["version"], field: "Reviewed latest and retried"})
    assert retried.status_code == 200
    assert retried.json()["version"] == 3
    assert db_client.get(path).json() == retried.json()


@pytest.mark.parametrize("method", ["put", "patch"])
@pytest.mark.parametrize("version", [None, 0, -1, "1", True, 1.5])
def test_invalid_versions_cannot_mutate_resource(db_client, resource, method, version):
    collection, original, payload = resource
    path = f"{collection}/{original['id']}"
    result = db_client.request(method, path, json={**payload, "version": version})
    assert result.status_code == 422
    assert any(item["loc"] == ["body", "version"] for item in result.json()["detail"])
    assert db_client.get(path).json() == original


@pytest.mark.parametrize("method", ["put", "patch"])
def test_missing_version_is_not_an_unconditional_write(db_client, resource, method):
    collection, original, payload = resource
    path = f"{collection}/{original['id']}"
    result = db_client.request(method, path, json=payload)
    assert result.status_code == 422
    assert any(item["loc"] == ["body", "version"] for item in result.json()["detail"])
    assert db_client.get(path).json() == original


def test_no_business_change_still_consumes_revision(db_client, resource):
    collection, original, payload = resource
    path = f"{collection}/{original['id']}"
    changed = db_client.patch(path, json={"version": 1})
    assert changed.status_code == 200
    assert changed.json() == {**original, "version": 2}
    assert db_client.patch(path, json={"version": 1}).status_code == 409
    replaced = db_client.put(path, json={**payload, "version": 2})
    assert replaced.status_code == 200
    assert replaced.json() == {**original, "version": 3}


def test_client_cannot_choose_initial_version(db_client, resource):
    collection, original, payload = resource
    before = db_client.get(collection).json()
    assert db_client.post(collection, json={**payload, "version": 50}).status_code == 422
    assert db_client.get(collection).json() == before


def test_delete_requires_current_revision(db_client, resource):
    collection, original, _ = resource
    path = f"{collection}/{original['id']}"
    assert db_client.delete(path).status_code == 422
    for value in (0, -1, "invalid"):
        assert db_client.delete(path, params={"version": value}).status_code == 422
    assert db_client.patch(path, json={"version": 1}).status_code == 200
    assert db_client.delete(path, params={"version": 1}).status_code == 409
    assert db_client.get(path).json() == {**original, "version": 2}
    assert db_client.delete(path, params={"version": 2}).status_code == 204
    assert db_client.delete(path, params={"version": 2}).status_code == 404


@pytest.fixture
def committed_resources(test_engine):
    """Real committed rows for independent connections; clean up only these IDs."""
    marker = uuid4().hex
    ids = {}
    try:
        with Session(test_engine, expire_on_commit=False) as session:
            user = User(username="race_" + marker, password_hash=password_hasher.hash("Concurrent writer test password"))
            session.add(user)
            session.flush()
            ids["user"] = user.id
            company = Company(name="Race company " + marker, owner_id=user.id)
            session.add(company)
            session.flush()
            ids["company"] = company.id
            job = Job(title="Race job", company_id=company.id, location="Remote")
            session.add(job)
            session.flush()
            ids["job"] = job.id
            session.commit()
        yield ids
    finally:
        with test_engine.begin() as connection:
            if "job" in ids:
                connection.execute(delete(OutboxEvent).where(OutboxEvent.payload["job_id"].as_integer() == ids["job"]))
                connection.execute(delete(Job).where(Job.id == ids["job"]))
            if "company" in ids:
                connection.execute(delete(Company).where(Company.id == ids["company"]))
            if "user" in ids:
                connection.execute(delete(User).where(User.id == ids["user"]))


@pytest.mark.parametrize("collection, model, key, field", [
    ("jobs", Job, "job", "title"), ("companies", Company, "company", "name"),
])
@pytest.mark.parametrize("method", ["PUT", "PATCH"])
def test_truly_concurrent_requests_have_one_winner(test_engine, committed_resources, collection, model, key, field, method):
    ids = committed_resources
    path = f"/{collection}/{ids[key]}"
    barrier = Barrier(2)

    def synchronize(session, flush_context, instances):
        if any(isinstance(item, model) and item.id == ids[key] for item in session.dirty):
            barrier.wait(timeout=10)

    def database():
        with Session(test_engine) as session:
            yield session

    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = database
    event.listen(Session, "before_flush", synchronize)
    try:
        with TestClient(app) as client:
            client.headers["Authorization"] = "Bearer " + create_access_token(ids["user"])
            original = client.get(path).json()
            payload = {"company_id": ids["company"], "location": "Remote"} if collection == "jobs" else {}
            def write(label):
                return client.request(method, path, json={**payload, "version": 1, field: "Concurrent " + label})
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(write, label) for label in ("left", "right")]
                results = [future.result(timeout=20) for future in futures]
            assert sorted(result.status_code for result in results) == [200, 409]
            winner = next(result.json() for result in results if result.status_code == 200)
            assert winner["version"] == original["version"] + 1
            assert client.get(path).json() == winner
            assert next(result.json() for result in results if result.status_code == 409) == {"detail": CONFLICT_DETAIL}
            with test_engine.connect() as connection:
                assert connection.scalar(select(model.version).where(model.id == ids[key])) == 2
                assert connection.scalar(select(getattr(model, field)).where(model.id == ids[key])) == winner[field]
                assert connection.scalar(select(func.count()).select_from(OutboxEvent)) == 0
    finally:
        event.remove(Session, "before_flush", synchronize)
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.mark.parametrize("model, key, field", [(Job, "job", "title"), (Company, "company", "name")])
def test_stale_orm_delete_cannot_remove_newer_update(test_engine, committed_resources, model, key, field):
    identity = committed_resources[key]
    with Session(test_engine) as first, Session(test_engine) as stale:
        winner = first.get(model, identity)
        outdated = stale.get(model, identity)
        setattr(winner, field, "Newer committed value")
        first.commit()
        stale.delete(outdated)
        with pytest.raises(StaleDataError):
            stale.commit()
        stale.rollback()
    with Session(test_engine) as check:
        assert getattr(check.get(model, identity), field) == "Newer committed value"
        assert check.get(model, identity).version == 2
