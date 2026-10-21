import pytest


@pytest.fixture
def company(db_client):
    response = db_client.post("/companies", json={
        "name": "WorkWise", "description": "Original description",
    })
    assert response.status_code == 201
    return response.json()


def test_put_replaces_company_and_can_clear_description(db_client, company):
    path = f"/companies/{company['id']}"
    payload = {"name": "WorkWise Labs", "description": "Updated description"}

    response = db_client.put(path, json=payload)

    assert response.status_code == 200
    assert response.json() == {"id": company["id"], **payload}
    assert db_client.get(path).json() == response.json()

    cleared = db_client.put(path, json={"name": "WorkWise Labs"})
    assert cleared.status_code == 200
    expected = {"id": company["id"], "name": "WorkWise Labs", "description": None}
    assert cleared.json() == expected
    assert db_client.get(path).json() == expected
    assert db_client.get("/companies").json() == [expected]


def test_delete_unlinked_company_returns_empty_204(db_client, company):
    path = f"/companies/{company['id']}"

    response = db_client.delete(path)

    assert response.status_code == 204
    assert response.content == b""
    assert db_client.get(path).status_code == 404
    assert db_client.get("/companies").json() == []
    assert db_client.delete(path).status_code == 404


def test_patch_preserves_omitted_fields_and_can_clear_description(db_client, company):
    path = f"/companies/{company['id']}"
    renamed = db_client.patch(path, json={"name": "WorkWise Labs"})
    expected = {**company, "name": "WorkWise Labs"}
    assert renamed.status_code == 200
    assert renamed.json() == expected

    for description in ("Updated description", None):
        response = db_client.patch(path, json={"description": description})
        expected = {**expected, "description": description}
        assert response.status_code == 200
        assert response.json() == expected
        assert db_client.get(path).json() == expected

    unchanged = db_client.patch(path, json={})
    assert unchanged.status_code == 200
    assert unchanged.json() == expected


@pytest.mark.parametrize("method", ["put", "patch"])
def test_rename_updates_linked_job_names_and_filters(db_client, company, method):
    jobs = []
    for title in ("First job", "Second job"):
        created = db_client.post("/jobs", json={
            "title": title, "company_id": company["id"], "location": "Remote",
        })
        assert created.status_code == 201
        jobs.append(created.json())

    renamed = db_client.request(method, f"/companies/{company['id']}", json={
        "name": "Renamed Company", "description": company["description"],
    })

    assert renamed.status_code == 200
    assert renamed.json() == {**company, "name": "Renamed Company"}
    expected = [{**job, "company": "Renamed Company"} for job in jobs]
    assert db_client.get("/jobs").json() == expected
    for job in expected:
        assert db_client.get(f"/jobs/{job['id']}").json() == job
    assert db_client.get("/jobs", params={"company": "WorkWise"}).json() == []
    assert db_client.get("/jobs", params={"company": "Renamed Company"}).json() == expected
    assert db_client.get("/jobs", params={"company_id": company["id"]}).json() == expected


@pytest.mark.parametrize("method", ["put", "patch"])
def test_duplicate_rename_rolls_back_all_changes(db_client, company, method):
    other = db_client.post("/companies", json={"name": "Taken name"})
    assert other.status_code == 201
    job = db_client.post("/jobs", json={
        "title": "Developer", "company_id": company["id"], "location": "Remote",
    })
    assert job.status_code == 201

    response = db_client.request(method, f"/companies/{company['id']}", json={
        "name": "Taken name", "description": "Must also roll back",
    })

    assert response.status_code == 409
    assert response.json() == {"detail": "Company name already exists"}
    assert db_client.get(f"/companies/{company['id']}").json() == company
    assert db_client.get(f"/companies/{other.json()['id']}").json() == other.json()
    assert db_client.get("/jobs").json() == [job.json()]
    # A failed transaction must not poison later writes.
    assert db_client.patch(f"/companies/{company['id']}", json={"name": company["name"]}).status_code == 200


@pytest.mark.parametrize("method", ["put", "patch"])
@pytest.mark.parametrize("changes", [
    {"name": None}, {"name": ""}, {"name": "   "}, {"name": "\t\n"},
    {"name": "x" * 201}, {"name": 123}, {"description": 123}, {"id": 42},
])
def test_invalid_updates_leave_company_unchanged(db_client, company, method, changes):
    path = f"/companies/{company['id']}"
    payload = {"name": company["name"], **changes}

    response = db_client.request(method, path, json=payload)

    assert response.status_code == 422
    assert db_client.get(path).json() == company


def test_put_requires_name(db_client, company):
    path = f"/companies/{company['id']}"
    response = db_client.put(path, json={"description": "Only description"})
    assert response.status_code == 422
    assert db_client.get(path).json() == company


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_mutating_missing_company_returns_404(db_client, method):
    kwargs = {} if method == "delete" else {"json": {"name": "Missing Company"}}
    response = db_client.request(method, "/companies/2147483647", **kwargs)
    assert response.status_code == 404
    assert response.json() == {"detail": "Company not found"}


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_mutation_rejects_non_integer_company_id(db_client, method):
    kwargs = {} if method == "delete" else {"json": {"name": "Valid name"}}
    assert db_client.request(method, "/companies/abc", **kwargs).status_code == 422


def test_linked_company_delete_conflict_preserves_jobs_and_allows_retry(db_client, company):
    path = f"/companies/{company['id']}"
    jobs = []
    for title in ("First job", "Second job"):
        response = db_client.post("/jobs", json={
            "title": title, "company_id": company["id"], "location": "Remote",
        })
        assert response.status_code == 201
        jobs.append(response.json())

    blocked = db_client.delete(path)
    assert blocked.status_code == 409
    assert blocked.json() == {"detail": "Company has jobs and cannot be deleted"}
    assert db_client.get(path).json() == company
    assert db_client.get("/jobs").json() == jobs
    assert db_client.patch(path, json={"description": company["description"]}).status_code == 200

    assert db_client.delete(f"/jobs/{jobs[0]['id']}").status_code == 204
    assert db_client.delete(path).status_code == 409
    assert db_client.delete(f"/jobs/{jobs[1]['id']}").status_code == 204
    assert db_client.delete(path).status_code == 204
    assert db_client.get(path).status_code == 404
    assert db_client.get("/jobs").json() == []
