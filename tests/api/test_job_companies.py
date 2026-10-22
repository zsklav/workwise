import pytest
from psycopg.errors import ForeignKeyViolation
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from workwise.db.models import Company, Job


@pytest.fixture
def company_pair(db_client):
    companies = []
    for name in ("First Company", "Second Company"):
        response = db_client.post("/companies", json={"name": name})
        assert response.status_code == 201
        companies.append(response.json())
    return companies


@pytest.mark.parametrize("method", ["put", "patch"])
def test_job_can_move_to_another_company(db_client, company_pair, method):
    first, second = company_pair
    payload = {"title": "Developer", "company_id": first["id"], "location": "Remote"}
    created = db_client.post("/jobs", json=payload)
    assert created.status_code == 201
    job_id = created.json()["id"]
    changes = {"company_id": second["id"]}
    if method == "put":
        changes = {**payload, **changes}

    response = db_client.request(method, f"/jobs/{job_id}", json={"version": 1, **changes})

    assert response.status_code == 200
    expected = {
        "id": job_id, "version": 2, **payload, "company_id": second["id"],
        "company": second["name"], "description": None,
    }
    assert response.json() == expected
    assert db_client.get(f"/jobs/{job_id}").json() == expected
    assert db_client.get("/jobs", params={"company_id": first["id"]}).json() == []
    assert db_client.get("/jobs", params={"company_id": second["id"]}).json() == [expected]
    assert db_client.get("/companies").json() == company_pair


@pytest.mark.parametrize("method", ["post", "put", "patch"])
def test_missing_company_rejected_without_changing_job(db_client, company_pair, method):
    first, second = company_pair
    payload = {"title": "Developer", "company_id": first["id"], "location": "Remote"}
    original = db_client.post("/jobs", json=payload)
    assert original.status_code == 201
    job_id = original.json()["id"]
    missing_id = max(first["id"], second["id"]) + 1000
    invalid = {**payload, "company_id": missing_id, "title": "Should not change"}
    path = "/jobs" if method == "post" else f"/jobs/{job_id}"

    if method in ("put", "patch"):
        invalid["version"] = 1
    response = db_client.request(method, path, json=invalid)

    assert response.status_code == 404
    assert response.json() == {"detail": "Company not found"}
    assert db_client.get("/jobs").json() == [original.json()]


@pytest.mark.parametrize("method", ["post", "put", "patch"])
@pytest.mark.parametrize("value", [None, 0, -1, "1", True, 1.5])
def test_invalid_company_ids_rejected(db_client, company_pair, method, value):
    payload = {"title": "Developer", "company_id": company_pair[0]["id"], "location": "Remote"}
    created = db_client.post("/jobs", json=payload)
    assert created.status_code == 201
    path = "/jobs" if method == "post" else f"/jobs/{created.json()['id']}"

    if method in ("put", "patch"):
        payload["version"] = 1
    response = db_client.request(method, path, json={**payload, "company_id": value})

    assert response.status_code == 422
    assert db_client.get("/jobs").json() == [created.json()]


@pytest.mark.parametrize("method", ["post", "put", "patch"])
def test_old_company_text_input_is_explicitly_rejected(db_client, company_pair, method):
    payload = {"title": "Developer", "company_id": company_pair[0]["id"], "location": "Remote"}
    created = db_client.post("/jobs", json=payload)
    assert created.status_code == 201
    path = "/jobs" if method == "post" else f"/jobs/{created.json()['id']}"

    if method in ("put", "patch"):
        payload["version"] = 1
    response = db_client.request(method, path, json={**payload, "company": "Ignored?"})

    assert response.status_code == 422
    assert db_client.get("/jobs").json() == [created.json()]


def test_name_and_id_filters_intersect_before_pagination(db_client, company_pair):
    first, second = company_pair
    jobs = []
    for company in (second, first, first):
        response = db_client.post("/jobs", json={
            "title": "Developer", "company_id": company["id"], "location": "Remote",
        })
        assert response.status_code == 201
        jobs.append(response.json())
    assert db_client.get("/jobs", params={
        "company_id": first["id"], "company": first["name"], "limit": 1, "offset": 1,
    }).json() == jobs[2:]
    assert db_client.get("/jobs", params={
        "company_id": first["id"], "company": second["name"],
    }).json() == []
    assert db_client.get("/jobs", params={"company_id": second["id"] + 1000}).json() == []


@pytest.mark.parametrize("value", [0, -1, "abc"])
def test_invalid_company_id_filter(db_client, value):
    assert db_client.get("/jobs", params={"company_id": value}).status_code == 422


def test_database_enforces_company_reference_and_restricts_deletion(db_session):
    company = Company(name="Linked Company")
    job = Job(title="Developer", employer=company, location="Remote")
    db_session.add(job)
    db_session.commit()

    with pytest.raises(IntegrityError) as error:
        with db_session.begin_nested():
            db_session.execute(delete(Company).where(Company.id == company.id))
    assert isinstance(error.value.orig, ForeignKeyViolation)
    assert error.value.orig.diag.constraint_name == "fk_jobs_company_id_companies"
    assert db_session.scalar(select(Job.company_id).where(Job.id == job.id)) == company.id

    with pytest.raises(IntegrityError) as error:
        with db_session.begin_nested():
            db_session.execute(text("UPDATE public.jobs SET company_id = :id WHERE id = :job_id"), {
                "id": company.id + 1000, "job_id": job.id,
            })
    assert error.value.orig.diag.constraint_name == "fk_jobs_company_id_companies"

    with pytest.raises(IntegrityError) as error:
        with db_session.begin_nested():
            db_session.execute(text("UPDATE public.jobs SET company_id = NULL WHERE id = :id"), {"id": job.id})
    assert error.value.orig.diag.column_name == "company_id"


def test_deleting_a_job_keeps_its_company(db_client, company_pair):
    company = company_pair[0]
    created = db_client.post("/jobs", json={
        "title": "Developer", "company_id": company["id"], "location": "Remote",
    })
    assert created.status_code == 201
    assert db_client.delete(f"/jobs/{created.json()['id']}", params={"version": 1}).status_code == 204
    assert db_client.get(f"/companies/{company['id']}").json() == company
