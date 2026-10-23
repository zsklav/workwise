from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.orm import Session

from workwise.api.main import app
from workwise.api.routes import applications, jobs
from workwise.core.security import create_access_token
from workwise.db.models import Company, IdempotencyRecord, Job, JobApplication, OutboxEvent, User
from workwise.db.session import get_db


@pytest.fixture
def actors(db_session, anonymous_client):
    users = {}
    for name, role in (("employer", "member"), ("candidate", "member"), ("outsider", "member"), ("reviewadmin", "admin")):
        user = User(username=name, password_hash="fixture-not-for-login", role=role)
        db_session.add(user)
        db_session.flush()
        users[name] = user
    company = Company(name="Applications company", owner_id=users["employer"].id)
    db_session.add(company)
    db_session.flush()
    job = Job(title="Backend engineer", company_id=company.id, location="Remote")
    db_session.add(job)
    db_session.commit()
    return anonymous_client, users, company, job


def headers(users, name, key=None):
    result = {"Authorization": "Bearer " + create_access_token(users[name].id)}
    if key is not None:
        result["Idempotency-Key"] = key
    return result


def job_payload(company):
    return {"title": "Idempotent job", "company_id": company.id, "location": "Remote"}


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_job_retry_is_atomic_and_replays_original_snapshot_even_after_edit(actors, db_session):
    client, users, company, _ = actors
    auth = headers(users, "employer", "request-123")
    first = client.post("/jobs", headers=auth, json=job_payload(company))
    assert first.status_code == 201 and first.headers["Idempotency-Replayed"] == "false"
    changed = client.patch(f"/jobs/{first.json()['id']}", headers=auth, json={"version": 1, "title": "Edited"})
    assert changed.status_code == 200
    retry = client.post("/jobs", headers=auth, json={**job_payload(company), "description": None})
    assert retry.status_code == 201 and retry.headers["Idempotency-Replayed"] == "true"
    assert retry.json() == first.json()
    assert count(db_session, Job) == 2 and count(db_session, OutboxEvent) == 1
    record = db_session.scalar(select(IdempotencyRecord))
    assert record.key_hash != "request-123" and len(record.key_hash) == 64
    assert record.response_body == first.json()


def test_changed_body_cannot_reuse_key_and_expired_key_can_be_reused(actors, db_session):
    client, users, company, _ = actors
    auth = headers(users, "employer", "reusable")
    first = client.post("/jobs", headers=auth, json=job_payload(company))
    payload = {**job_payload(company), "title": "Changed"}
    assert client.post("/jobs", headers=auth, json=payload).status_code == 409
    record = db_session.scalar(select(IdempotencyRecord))
    record.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()
    later = client.post("/jobs", headers=auth, json=payload)
    assert later.status_code == 201 and later.json()["id"] != first.json()["id"]
    assert count(db_session, IdempotencyRecord) == 1 and count(db_session, OutboxEvent) == 2


def test_identical_unkeyed_posts_express_two_creation_intents(actors, db_session):
    client, users, company, _ = actors
    results = [client.post("/jobs", headers=headers(users, "employer"), json=job_payload(company)) for _ in range(2)]
    assert all(r.status_code == 201 for r in results)
    assert results[0].json()["id"] != results[1].json()["id"]
    assert count(db_session, IdempotencyRecord) == 0 and count(db_session, OutboxEvent) == 2


@pytest.mark.parametrize("key", ["", "bad key", "bad@key", "a" * 129])
def test_invalid_keys_cannot_create_jobs(actors, db_session, key):
    client, users, company, _ = actors
    response = client.post("/jobs", headers=headers(users, "employer", key), json=job_payload(company))
    assert response.status_code == 422
    assert all("input" not in item for item in response.json()["detail"])
    assert count(db_session, Job) == 1 and count(db_session, IdempotencyRecord) == 0


def test_key_is_scoped_to_actor_and_failed_authorization_does_not_reserve_it(actors, db_session):
    client, users, company, _ = actors
    first = client.post("/jobs", headers=headers(users, "employer", "same"), json=job_payload(company))
    assert first.status_code == 201
    assert client.post("/jobs", headers=headers(users, "outsider", "same"), json=job_payload(company)).status_code == 403
    other = Company(name="Other employer", owner_id=users["outsider"].id)
    db_session.add(other)
    db_session.commit()
    assert client.post("/jobs", headers=headers(users, "outsider", "same"), json=job_payload(other)).status_code == 201
    assert count(db_session, IdempotencyRecord) == 2


def test_outbox_failure_rolls_back_job_and_idempotency_record(actors, db_session, monkeypatch):
    client, users, company, _ = actors
    def fail():
        raise RuntimeError("outbox fixture failure")
    monkeypatch.setattr(jobs, "capture_context", fail)
    assert client.post("/jobs", headers=headers(users, "employer", "rollback"), json=job_payload(company)).status_code == 500
    assert count(db_session, Job) == 1
    assert count(db_session, OutboxEvent) == count(db_session, IdempotencyRecord) == 0


def test_application_flow_is_private_versioned_and_emits_one_event_per_transition(actors, db_session):
    client, users, _, job = actors
    candidate, owner = headers(users, "candidate", "apply-1"), headers(users, "employer")
    created = client.post(f"/jobs/{job.id}/applications", headers=candidate, json={"cover_letter": "Private letter"})
    assert created.status_code == 201 and created.json()["status"] == "applied"
    application = created.json()
    path = f"/applications/{application['id']}"
    assert client.get(path, headers=candidate).json() == application
    assert client.get("/applications/mine", headers=candidate).json() == [application]
    assert client.get(f"/jobs/{job.id}/applications", headers=owner).json() == [application]
    assert client.get(path, headers=headers(users, "outsider")).status_code == 404
    assert client.get(f"/jobs/{job.id}/applications", headers=candidate).status_code == 403
    assert client.patch(path, headers=candidate, json={"version": 1, "status": "shortlisted"}).status_code == 403
    shortlisted = client.patch(path, headers=owner, json={"version": 1, "status": "shortlisted"})
    assert shortlisted.status_code == 200 and shortlisted.json()["version"] == 2
    assert client.patch(path, headers=owner, json={"version": 1, "status": "rejected"}).status_code == 409
    hired = client.patch(path, headers=owner, json={"version": 2, "status": "hired"})
    assert hired.status_code == 200 and hired.json()["version"] == 3
    assert client.patch(path, headers=owner, json={"version": 3, "status": "rejected"}).status_code == 409
    assert client.get("/applications/mine?status=hired", headers=candidate).json() == [hired.json()]
    replay = client.post(f"/jobs/{job.id}/applications", headers=candidate, json={"cover_letter": "Private letter"})
    assert replay.json() == application and replay.headers["Idempotency-Replayed"] == "true"
    events = list(db_session.scalars(select(OutboxEvent)))
    assert len(events) == 3
    assert {e.payload["status"] for e in events} == {"applied", "shortlisted", "hired"}
    assert all(e.payload["recipient_id"] == users["candidate"].id and "Private letter" not in str(e.payload) for e in events)


def test_application_uniqueness_own_company_and_terminal_rejection(actors, db_session):
    client, users, _, job = actors
    path = f"/jobs/{job.id}/applications"
    assert client.post(path, headers=headers(users, "employer"), json={}).status_code == 409
    first = client.post(path, headers=headers(users, "candidate"), json={}).json()
    assert client.post(path, headers=headers(users, "candidate"), json={}).status_code == 409
    review = f"/applications/{first['id']}"
    owner = headers(users, "employer")
    assert client.patch(review, headers=owner, json={"version": 1, "status": "hired"}).status_code == 409
    assert client.patch(review, headers=headers(users, "reviewadmin"), json={"version": 1, "status": "rejected"}).status_code == 200
    assert client.patch(review, headers=owner, json={"version": 2, "status": "shortlisted"}).status_code == 409
    assert count(db_session, JobApplication) == 1 and count(db_session, OutboxEvent) == 2


@pytest.mark.parametrize("payload", [{"applicant_id": 999}, {"status": "hired"}, {"cover_letter": "secret" * 700}])
def test_application_input_rejects_spoofing_and_redacts_private_data(actors, payload):
    client, users, _, job = actors
    result = client.post(f"/jobs/{job.id}/applications", headers=headers(users, "candidate"), json=payload)
    assert result.status_code == 422
    assert all("input" not in error and "ctx" not in error for error in result.json()["detail"])


def test_application_protects_job_deletion_and_company_transfer(actors, db_session):
    client, users, _, job = actors
    assert client.post(f"/jobs/{job.id}/applications", headers=headers(users, "candidate"), json={}).status_code == 201
    owner = headers(users, "employer")
    assert client.delete(f"/jobs/{job.id}?version=1", headers=owner).status_code == 409
    other = Company(name="Transfer destination", owner_id=users["employer"].id)
    db_session.add(other)
    db_session.commit()
    assert client.patch(f"/jobs/{job.id}", headers=owner, json={"version": 1, "company_id": other.id}).status_code == 409
    assert client.put(f"/jobs/{job.id}", headers=owner, json={**job_payload(other), "version": 1}).status_code == 409
    assert client.patch(f"/jobs/{job.id}", headers=owner, json={"version": 1, "title": "Updated description"}).status_code == 200


def test_application_event_failure_rolls_back_creation_and_retry_key(actors, db_session, monkeypatch):
    client, users, _, job = actors
    def fail():
        raise RuntimeError("application event unavailable")
    monkeypatch.setattr(applications, "capture_context", fail)
    result = client.post(f"/jobs/{job.id}/applications", headers=headers(users, "candidate", "rollback"), json={})
    assert result.status_code == 500
    assert count(db_session, JobApplication) == count(db_session, OutboxEvent) == count(db_session, IdempotencyRecord) == 0


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "status=invalid"])
def test_application_lists_validate_pagination_and_status(actors, query):
    client, users, _, job = actors
    assert client.get("/applications/mine?" + query, headers=headers(users, "candidate")).status_code == 422
    assert client.get(f"/jobs/{job.id}/applications?" + query, headers=headers(users, "employer")).status_code == 422


def test_application_views_hide_other_users_and_preserve_paginated_order(actors):
    client, users, _, job = actors
    one = client.post(f"/jobs/{job.id}/applications", headers=headers(users, "candidate"), json={}).json()
    two = client.post(f"/jobs/{job.id}/applications", headers=headers(users, "outsider"), json={}).json()
    assert client.get("/applications/mine", headers=headers(users, "candidate")).json() == [one]
    assert client.get("/applications/mine", headers=headers(users, "outsider")).json() == [two]
    assert client.get(f"/jobs/{job.id}/applications?limit=1&offset=1", headers=headers(users, "employer")).json() == [two]
    assert client.get("/applications/mine").status_code == 401
    assert client.post(f"/jobs/{job.id}/applications", json={}).status_code == 401
    assert client.get("/applications/999999", headers=headers(users, "candidate")).status_code == 404


@pytest.fixture
def committed_upgrade(test_engine):
    ids = {}
    marker = uuid4().hex
    try:
        with Session(test_engine) as db:
            owner = User(username="owner_" + marker, password_hash="test")
            candidate = User(username="cand_" + marker, password_hash="test")
            db.add_all([owner, candidate]); db.flush()
            ids.update(owner=owner.id, candidate=candidate.id)
            company = Company(name="Concurrent upgrades " + marker, owner_id=owner.id)
            db.add(company); db.flush(); ids["company"] = company.id
            job = Job(title="Concurrency fixture", company_id=company.id, location="Remote")
            db.add(job); db.flush(); ids["job"] = job.id
            application = JobApplication(job_id=job.id, applicant_id=candidate.id)
            db.add(application); db.flush(); ids["application"] = application.id
            db.commit()
        yield ids
    finally:
        with test_engine.begin() as c:
            c.execute(delete(OutboxEvent).where(OutboxEvent.payload["company_id"].as_integer() == ids.get("company", -1)))
            c.execute(delete(JobApplication).where(JobApplication.applicant_id == ids.get("candidate", -1)))
            c.execute(delete(Job).where(Job.company_id == ids.get("company", -1)))
            c.execute(delete(Company).where(Company.id == ids.get("company", -1)))
            c.execute(delete(User).where(User.id.in_([ids.get("owner", -1), ids.get("candidate", -1)])))


@pytest.mark.parametrize("operation", ["keyed_job", "review", "duplicate_apply", "apply_transfer"])
def test_concurrent_upgrade_requests_have_atomic_database_effects(test_engine, committed_upgrade, operation):
    ids = committed_upgrade
    barrier = Barrier(2)
    errors = []
    def database():
        with Session(test_engine) as db:
            try:
                yield db
            except Exception as error:
                errors.append((type(error).__name__, getattr(getattr(error, "orig", None), "sqlstate", None), getattr(error, "statement", None)))
                raise
    def synchronize(db, context, instances):
        if operation == "review" and any(isinstance(x, JobApplication) for x in db.dirty):
            barrier.wait(timeout=10)
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = database
    event.listen(Session, "before_flush", synchronize)
    try:
        def send(index):
            with TestClient(app) as client:
                actor = ids["owner"]
                if operation != "review":
                    barrier.wait(timeout=10)
                auth = {"Authorization": "Bearer " + create_access_token(actor)}
                if operation == "keyed_job":
                    return client.post("/jobs", headers={**auth, "Idempotency-Key": "same-race-key"},
                                       json={"title": "Single creation", "company_id": ids["company"], "location": "Remote"})
                if operation == "review":
                    return client.patch(f"/applications/{ids['application']}", headers=auth,
                                        json={"version": 1, "status": "shortlisted" if index == 0 else "rejected"})
                if operation == "apply_transfer" and index == 1:
                    return client.patch(f"/jobs/{ids['job']}", headers=auth,
                                        json={"version": 1, "company_id": ids["destination"]})
                return client.post(f"/jobs/{ids['job']}/applications", headers={"Authorization": "Bearer " + create_access_token(ids["candidate"])}, json={})
        if operation in ("duplicate_apply", "apply_transfer"):
            with test_engine.begin() as c:
                c.execute(delete(JobApplication).where(JobApplication.id == ids["application"]))
                if operation == "apply_transfer":
                    ids["destination"] = c.scalar(Company.__table__.insert().values(name="Transfer " + uuid4().hex, owner_id=ids["owner"]).returning(Company.id))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(send, range(2)))
        if operation == "apply_transfer":
            assert results[0].status_code == 201 and results[1].status_code in (200, 409), errors
            with Session(test_engine) as db:
                final_company = db.get(Job, ids["job"]).company_id
                assert final_company == (ids["destination"] if results[1].status_code == 200 else ids["company"])
                envelopes = list(db.scalars(select(OutboxEvent).where(OutboxEvent.payload["application_id"].as_integer() == results[0].json()["id"])))
                assert len(envelopes) == 1 and envelopes[0].payload["company_id"] == final_company
            return
        expected = [201, 201] if operation == "keyed_job" else ([200, 409] if operation == "review" else [201, 409])
        assert sorted(r.status_code for r in results) == expected
        if operation == "keyed_job":
            assert results[0].json() == results[1].json()
            assert {r.headers["Idempotency-Replayed"] for r in results} == {"false", "true"}
        with Session(test_engine) as db:
            assert db.scalar(select(func.count()).select_from(OutboxEvent).where(OutboxEvent.payload["company_id"].as_integer() == ids["company"])) == 1
    finally:
        event.remove(Session, "before_flush", synchronize)
        app.dependency_overrides.clear(); app.dependency_overrides.update(previous)
        if "destination" in ids:
            # The shared fixture removes the application and job afterwards.
            with test_engine.begin() as c:
                c.execute(Job.__table__.update().where(Job.id == ids["job"]).values(company_id=ids["company"]))
                c.execute(delete(OutboxEvent).where(OutboxEvent.payload["company_id"].as_integer() == ids["destination"]))
                c.execute(delete(Company).where(Company.id == ids["destination"]))


def test_applying_waits_for_job_transfer_then_reads_the_current_company(test_engine, committed_upgrade):
    ids = committed_upgrade
    ready, pid = Event(), []
    with test_engine.begin() as c:
        c.execute(delete(JobApplication).where(JobApplication.id == ids["application"]))
        destination = c.scalar(Company.__table__.insert().values(name="Locked transfer " + uuid4().hex, owner_id=ids["owner"]).returning(Company.id))
    try:
        with test_engine.connect() as writer:
            transaction = writer.begin()
            writer.execute(Job.__table__.update().where(Job.id == ids["job"]).values(company_id=destination, version=2))
            def reader():
                with Session(test_engine) as db:
                    pid.append(db.scalar(text("SELECT pg_backend_pid()")))
                    ready.set()
                    job = applications.find_job(db, ids["job"], lock=True)
                    return job.company_id, job.employer.id
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(reader)
                try:
                    assert ready.wait(timeout=5)
                    deadline = time.monotonic() + 5
                    with test_engine.connect() as observer:
                        while observer.scalar(text("SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid"), {"pid": pid[0]}) != "Lock":
                            # pg_stat_activity snapshots cache within transactions.
                            observer.rollback()
                            assert time.monotonic() < deadline
                            time.sleep(.02)
                    transaction.commit()
                    assert future.result(timeout=5) == (destination, destination)
                finally:
                    if transaction.is_active:
                        transaction.rollback()
    finally:
        with test_engine.begin() as c:
            c.execute(Job.__table__.update().where(Job.id == ids["job"]).values(company_id=ids["company"]))
            c.execute(delete(Company).where(Company.id == destination))
