import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import Company


@pytest.fixture
def client(db_client: TestClient) -> TestClient:
    return db_client


def test_create_and_get_company(client: TestClient):
    payload = {"name": "WorkWise", "description": "A job management platform"}

    created = client.post("/companies", json=payload)

    assert created.status_code == 201
    company_id = created.json()["id"]
    expected = {"id": company_id, **payload}
    assert isinstance(company_id, int)
    assert created.json() == expected

    fetched = client.get(f"/companies/{company_id}")
    assert fetched.status_code == 200
    assert fetched.json() == expected
    assert client.get("/companies").json() == [expected]


def test_company_description_is_optional(client: TestClient):
    response = client.post("/companies", json={"name": "WorkWise"})

    assert response.status_code == 201
    assert response.json() == {
        "id": response.json()["id"], "name": "WorkWise", "description": None,
    }


def test_duplicate_company_returns_conflict_and_preserves_original(client: TestClient):
    original = client.post("/companies", json={
        "name": "WorkWise", "description": "Keep this description",
    })
    assert original.status_code == 201

    duplicate = client.post("/companies", json={
        "name": "WorkWise", "description": "Duplicate description",
    })

    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "Company name already exists"}
    assert client.get("/companies").json() == [original.json()]
    assert client.post("/companies", json={"name": "Another Company"}).status_code == 201


def test_company_names_are_case_sensitive(client: TestClient):
    first = client.post("/companies", json={"name": "WorkWise"})
    second = client.post("/companies", json={"name": "workwise"})

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]


@pytest.mark.parametrize("payload", [
    {},
    {"name": ""},
    {"name": "   "},
    {"name": "\t\n"},
    {"name": None},
    {"name": "x" * 201},
    {"name": "WorkWise", "description": 123},
    {"name": "WorkWise", "id": 1},
])
def test_invalid_company_is_not_created(client: TestClient, payload: dict):
    response = client.post("/companies", json=payload)

    assert response.status_code == 422
    assert client.get("/companies").json() == []


def test_missing_company_returns_404(client: TestClient):
    response = client.get("/companies/999")

    assert response.status_code == 404
    assert response.json() == {"detail": "Company not found"}


def test_company_id_must_be_integer(client: TestClient):
    assert client.get("/companies/abc").status_code == 422


def test_companies_are_paginated_in_id_order(client: TestClient):
    created = []
    for name in ("First Company", "Second Company", "Third Company"):
        response = client.post("/companies", json={"name": name})
        assert response.status_code == 201
        created.append(response.json())

    response = client.get("/companies", params={"limit": 1, "offset": 1})
    assert response.status_code == 200
    assert response.json() == created[1:2]
    assert client.get("/companies", params={"offset": 3}).json() == []


def test_companies_default_to_twenty_rows(client: TestClient, db_session: Session):
    companies = [Company(name=f"Company {number}") for number in range(21)]
    db_session.add_all(companies)
    db_session.flush()
    expected_ids = sorted(company.id for company in companies)
    db_session.commit()

    response = client.get("/companies")

    assert response.status_code == 200
    assert [company["id"] for company in response.json()] == expected_ids[:20]


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}])
def test_companies_reject_invalid_pagination(client: TestClient, params: dict):
    assert client.get("/companies", params=params).status_code == 422


def test_database_rejects_duplicate_company_names(db_session: Session):
    db_session.add(Company(name="WorkWise"))
    db_session.commit()
    db_session.add(Company(name="WorkWise"))

    with pytest.raises(IntegrityError) as error:
        db_session.flush()

    assert error.value.orig.diag.constraint_name == "uq_companies_name"
    db_session.rollback()
    assert db_session.scalar(select(func.count()).select_from(Company)) == 1


def test_creating_company_does_not_create_or_change_jobs(client: TestClient):
    company = client.post("/companies", json={"name": "WorkWise"})

    assert company.status_code == 201
    assert client.get("/jobs").json() == []
