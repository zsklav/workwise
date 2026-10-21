import pytest
import jwt
from sqlalchemy import select

from models import Company, User
from security import auth_settings, create_access_token


@pytest.fixture
def actors(db_session, auth_user):
    others = [User(username=name, password_hash=auth_user.password_hash, role=role, is_active=True)
              for name, role in (("other_member", "member"), ("site_admin", "admin"))]
    db_session.add_all(others)
    db_session.commit()
    return {
        "owner": {"Authorization": "Bearer " + create_access_token(auth_user.id)},
        "other": {"Authorization": "Bearer " + create_access_token(others[0].id)},
        "admin": {"Authorization": "Bearer " + create_access_token(others[1].id)},
    }


@pytest.fixture
def owned_resources(anonymous_client, actors):
    company = anonymous_client.post("/companies", json={"name": "Owned Company"}, headers=actors["owner"])
    assert company.status_code == 201
    job = anonymous_client.post("/jobs", json={
        "title": "Owned job", "company_id": company.json()["id"], "location": "Remote",
    }, headers=actors["owner"])
    assert job.status_code == 201
    return company.json(), job.json()


@pytest.mark.parametrize("method,path,payload", [
    ("post", "/companies", {"name": "Company"}),
    ("put", "/companies/1", {"name": "Company"}),
    ("patch", "/companies/1", {}), ("delete", "/companies/1", None),
    ("post", "/jobs", {"title": "Job", "company_id": 1, "location": "Remote"}),
    ("put", "/jobs/1", {"title": "Job", "company_id": 1, "location": "Remote"}),
    ("patch", "/jobs/1", {}), ("delete", "/jobs/1", None),
])
def test_all_writes_require_authentication(anonymous_client, method, path, payload):
    kwargs = {} if payload is None else {"json": payload}
    response = anonymous_client.request(method, path, **kwargs)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_public_reads_and_server_assigned_ownership(anonymous_client, actors, owned_resources, auth_user, db_session):
    company, job = owned_resources
    assert anonymous_client.get("/health").status_code == 200
    assert anonymous_client.get("/companies").json() == [company]
    assert anonymous_client.get(f"/companies/{company['id']}").json() == company
    assert anonymous_client.get("/jobs").json() == [job]
    assert anonymous_client.get(f"/jobs/{job['id']}").json() == job
    assert db_session.scalar(select(Company.owner_id).where(Company.id == company["id"])) == auth_user.id


@pytest.mark.parametrize("resource,method", [
    ("company", "put"), ("company", "patch"), ("company", "delete"),
    ("job", "put"), ("job", "patch"), ("job", "delete"),
])
def test_other_member_cannot_mutate_company_or_jobs(anonymous_client, actors, owned_resources, resource, method):
    company, job = owned_resources
    path = f"/companies/{company['id']}" if resource == "company" else f"/jobs/{job['id']}"
    payload = {"name": "Stolen company"} if resource == "company" else {
        "title": "Stolen job", "company_id": company["id"], "location": "Elsewhere",
    }
    kwargs = {} if method == "delete" else {"json": payload}
    response = anonymous_client.request(method, path, headers=actors["other"], **kwargs)
    assert response.status_code == 403
    assert anonymous_client.get("/companies").json() == [company]
    assert anonymous_client.get("/jobs").json() == [job]


def test_other_member_cannot_post_job_in_someone_elses_company(anonymous_client, actors, owned_resources):
    company, job = owned_resources
    response = anonymous_client.post("/jobs", json={
        "title": "Intruder job", "company_id": company["id"], "location": "Remote",
    }, headers=actors["other"])
    assert response.status_code == 403
    assert anonymous_client.get("/jobs").json() == [job]


@pytest.mark.parametrize("method", ["put", "patch"])
@pytest.mark.parametrize("direction", ["steal_source", "unauthorized_destination"])
def test_moving_jobs_checks_both_companies(anonymous_client, actors, owned_resources, method, direction):
    company, job = owned_resources
    other = anonymous_client.post("/companies", json={"name": "Other company"}, headers=actors["other"])
    assert other.status_code == 201
    actor = actors["other"] if direction == "steal_source" else actors["owner"]
    response = anonymous_client.request(method, f"/jobs/{job['id']}", json={
        "title": "Changed title", "company_id": other.json()["id"], "location": "Elsewhere",
    }, headers=actor)
    assert response.status_code == 403
    assert anonymous_client.get(f"/jobs/{job['id']}").json() == job


@pytest.mark.parametrize("method", ["post", "put", "patch"])
def test_owner_and_role_cannot_be_supplied_in_company_body(anonymous_client, actors, owned_resources, method):
    company, _ = owned_resources
    path = "/companies" if method == "post" else f"/companies/{company['id']}"
    response = anonymous_client.request(method, path, json={
        "name": "Company", "owner_id": 1, "role": "admin",
    }, headers=actors["owner"])
    assert response.status_code == 422
    assert anonymous_client.get(f"/companies/{company['id']}").json() == company


def test_admin_can_manage_other_members_company_and_jobs(anonymous_client, actors, owned_resources):
    company, job = owned_resources
    headers = actors["admin"]
    assert anonymous_client.put(f"/companies/{company['id']}", json={"name": "Admin rename"}, headers=headers).status_code == 200
    assert anonymous_client.patch(f"/companies/{company['id']}", json={"description": "Admin edit"}, headers=headers).status_code == 200
    assert anonymous_client.put(f"/jobs/{job['id']}", json={
        "title": "Admin edit", "company_id": company["id"], "location": "Remote",
    }, headers=headers).status_code == 200
    assert anonymous_client.patch(f"/jobs/{job['id']}", json={"title": "Admin patch"}, headers=headers).status_code == 200
    assert anonymous_client.delete(f"/companies/{company['id']}", headers=headers).status_code == 409
    assert anonymous_client.delete(f"/jobs/{job['id']}", headers=headers).status_code == 204
    assert anonymous_client.delete(f"/companies/{company['id']}", headers=headers).status_code == 204


def test_legacy_unowned_company_is_admin_only(anonymous_client, actors, db_session):
    company = Company(name="Legacy company")
    db_session.add(company)
    db_session.commit()
    path = f"/companies/{company.id}"
    payload = {"title": "Job", "company_id": company.id, "location": "Remote"}
    for actor in ("owner", "other"):
        assert anonymous_client.patch(path, json={}, headers=actors[actor]).status_code == 403
        assert anonymous_client.post("/jobs", json=payload, headers=actors[actor]).status_code == 403
        assert anonymous_client.delete(path, headers=actors[actor]).status_code == 403
    created = anonymous_client.post("/jobs", json=payload, headers=actors["admin"])
    assert created.status_code == 201
    assert anonymous_client.delete(f"/jobs/{created.json()['id']}", headers=actors["admin"]).status_code == 204
    assert anonymous_client.delete(path, headers=actors["admin"]).status_code == 204


def test_role_claim_in_a_token_cannot_override_database_role(anonymous_client, actors, owned_resources):
    company, _ = owned_resources
    token = actors["other"]["Authorization"].removeprefix("Bearer ")
    key = auth_settings.jwt_secret.get_secret_value()
    claims = jwt.decode(token, key, algorithms=["HS256"], audience=auth_settings.jwt_audience,
                        issuer=auth_settings.jwt_issuer)
    claims["role"] = "admin"
    token = jwt.encode(claims, key, algorithm="HS256")
    response = anonymous_client.patch(f"/companies/{company['id']}", json={"name": "Unauthorized rename"},
                                      headers={"Authorization": "Bearer " + token})
    assert response.status_code == 403
    assert anonymous_client.get(f"/companies/{company['id']}").json() == company
