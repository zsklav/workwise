from fastapi.testclient import TestClient
import pytest
from sqlalchemy.orm import Session

from workwise.db.models import Company, Job


@pytest.fixture
def seeded(db_client: TestClient, db_session: Session, auth_user) -> TestClient:
    company = Company(name="Search Co", owner_id=auth_user.id)
    db_session.add(company)
    db_session.flush()
    db_session.add_all([
        Job(title="Backend Engineer", location="Remote", company_id=company.id),
        Job(title="Data Analyst", location="Pune", description="SQL and 100% dashboards", company_id=company.id),
        Job(title="Frontend Engineer", location="Delhi", company_id=company.id),
    ])
    db_session.commit()
    return db_client


def titles(response):
    return [job["title"] for job in response.json()]


def test_search_matches_title_case_insensitively(seeded):
    response = seeded.get("/jobs", params={"q": "engineer"})
    assert response.status_code == 200
    assert titles(response) == ["Backend Engineer", "Frontend Engineer"]
    assert response.headers["X-Total-Count"] == "2"


def test_search_matches_location_and_description(seeded):
    assert titles(seeded.get("/jobs", params={"q": "pune"})) == ["Data Analyst"]
    assert titles(seeded.get("/jobs", params={"q": "dashboards"})) == ["Data Analyst"]


def test_search_treats_wildcards_as_text(seeded):
    assert titles(seeded.get("/jobs", params={"q": "%"})) == ["Data Analyst"]
    assert titles(seeded.get("/jobs", params={"q": "_"})) == []


def test_sort_and_total_count_ignore_pagination(seeded):
    response = seeded.get("/jobs", params={"sort": "-title", "limit": 1})
    assert titles(response) == ["Frontend Engineer"]
    assert response.headers["X-Total-Count"] == "3"


def test_invalid_sort_is_rejected(seeded):
    assert seeded.get("/jobs", params={"sort": "salary"}).status_code == 422


def test_security_headers_are_present(anonymous_client):
    response = anonymous_client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"


def test_v1_prefix_serves_the_same_api_and_unversioned_paths_still_work(seeded):
    assert seeded.get("/v1/jobs").json() == seeded.get("/jobs").json()
    assert seeded.get("/v1/jobs/deleted").status_code == 403
    paths = seeded.get("/openapi.json").json()["paths"]
    assert "/v1/jobs" in paths and "/jobs" not in paths


def test_job_details_round_trip_and_filters(seeded, db_session):
    company_id = seeded.get("/jobs").json()[0]["company_id"]
    created = seeded.post("/jobs", json={
        "title": "Platform Engineer", "company_id": company_id, "location": "Remote",
        "salary_min": 800000, "salary_max": 1500000, "remote": True, "job_type": "full_time"})
    assert created.status_code == 201
    body = created.json()
    assert (body["salary_min"], body["salary_max"], body["remote"], body["job_type"]) == (800000, 1500000, True, "full_time")

    assert titles(seeded.get("/jobs", params={"remote": "true"})) == ["Platform Engineer"]
    assert titles(seeded.get("/jobs", params={"job_type": "full_time"})) == ["Platform Engineer"]
    assert titles(seeded.get("/jobs", params={"min_salary": 1200000})) == ["Platform Engineer"]
    assert titles(seeded.get("/jobs", params={"min_salary": 2000000})) == []
    assert seeded.get("/jobs", params={"remote": "false"}).headers["X-Total-Count"] == "3"
    assert titles(seeded.get("/jobs", params={"sort": "-salary_max", "limit": 1})) == ["Platform Engineer"]


@pytest.mark.parametrize("extra", [
    {"salary_min": 10, "salary_max": 5},
    {"salary_min": -1},
    {"job_type": "forever"},
    {"remote": "yes"},
])
def test_invalid_job_details_are_rejected(seeded, extra):
    company_id = seeded.get("/jobs").json()[0]["company_id"]
    payload = {"title": "X", "company_id": company_id, "location": "Y", **extra}
    assert seeded.post("/jobs", json=payload).status_code == 422


def test_patch_cannot_break_the_salary_range(seeded):
    company_id = seeded.get("/jobs").json()[0]["company_id"]
    job = seeded.post("/jobs", json={"title": "Ranged", "company_id": company_id, "location": "Y",
                                     "salary_min": 100, "salary_max": 200}).json()
    response = seeded.patch(f"/jobs/{job['id']}", json={"version": 1, "salary_max": 50})
    assert response.status_code == 422
    assert seeded.patch(f"/jobs/{job['id']}", json={"version": 1, "salary_max": 300}).status_code == 200
