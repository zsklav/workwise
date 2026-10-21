from fastapi.testclient import TestClient
import pytest


@pytest.fixture
def client(db_client: TestClient) -> TestClient:
    return db_client

def test_health(client: TestClient):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "OK"}

def test_create_and_get_job(client: TestClient):
    payload = {
        "title": "Backend Intern",
        "company": "Example",
        "location": "Remote",
    }

    create_response = client.post("/jobs", json=payload)

    assert create_response.status_code == 201

    created_job = create_response.json()
    assert isinstance(created_job["id"], int)
    assert created_job == {"id": created_job["id"], **payload, "description": None}

    get_response = client.get(f"/jobs/{created_job['id']}")

    assert get_response.status_code == 200
    assert get_response.json() == created_job

def test_jobs_start_empty(client: TestClient):
    response = client.get("/jobs")

    assert response.status_code == 200
    assert response.json() == []

def test_get_missing_job(client: TestClient):
    response = client.get("/jobs/999")

    assert response.status_code == 404
    assert response.json() == {"detail": "Job not found"}

def test_delete_job(client: TestClient):
    payload = {
        "title": "Python Intern",
        "company": "Example",
        "location": "Remote",
    }

    create_response = client.post("/jobs", json=payload)
    assert create_response.status_code == 201
    job_id = create_response.json()["id"]

    delete_response = client.delete(f"/jobs/{job_id}")

    assert delete_response.status_code == 204
    assert delete_response.content == b""

    get_response = client.get(f"/jobs/{job_id}")

    assert get_response.status_code == 404

def test_update_job(client: TestClient):
    payload = {
        "title": "Python Intern",
        "company": "Example",
        "location": "Remote",
    }

    create_response = client.post("/jobs", json=payload)
    assert create_response.status_code == 201
    job_id = create_response.json()["id"]

    updated_payload = {
        "title": "Python Developer",
        "company": "Example",
        "location": "Bengaluru",
    }

    update_response = client.put(
        f"/jobs/{job_id}",
        json=updated_payload,
    )

    expected_job = {"id": job_id, **updated_payload, "description": None}

    assert update_response.status_code == 200
    assert update_response.json() == expected_job

    get_response = client.get(f"/jobs/{job_id}")

    assert get_response.status_code == 200
    assert get_response.json() == expected_job

    list_response = client.get("/jobs")

    assert list_response.status_code == 200
    assert list_response.json() == [expected_job]

@pytest.mark.parametrize(
    "payload",
    [
        {"company": "Example", "location": "Remote"},
        {"title": "", "company": "Example", "location": "Remote"},
    ],
)
def test_create_job_rejects_invalid_title(client: TestClient, payload):
    response = client.post("/jobs", json=payload)

    assert response.status_code == 422

    list_response = client.get("/jobs")

    assert list_response.status_code == 200
    assert list_response.json() == []


@pytest.mark.parametrize("method", ["put", "delete"])
def test_mutating_missing_job_returns_404(client: TestClient, method: str):
    if method == "put":
        response = client.put(
            "/jobs/999",
            json={"title": "Missing", "company": "Example", "location": "Remote"},
        )
    else:
        response = client.delete("/jobs/999")

    assert response.status_code == 404
    assert response.json() == {"detail": "Job not found"}
    assert client.get("/jobs").json() == []


def test_get_job_rejects_non_integer_id(client: TestClient):
    response = client.get("/jobs/abc")

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["path", "job_id"]


@pytest.mark.parametrize("description", ["Build and maintain Python APIs", None])
def test_create_job_with_optional_description(client: TestClient, description):
    payload = {
        "title": "Python Developer",
        "company": "Example",
        "location": "Remote",
        "description": description,
    }
    response = client.post("/jobs", json=payload)

    assert response.status_code == 201
    job_id = response.json()["id"]
    expected_job = {"id": job_id, **payload}
    assert response.json() == expected_job
    assert client.get(f"/jobs/{job_id}").json() == expected_job
    assert client.get("/jobs").json() == [expected_job]


def test_put_replaces_and_can_clear_description(client: TestClient):
    payload = {
        "title": "Python Developer",
        "company": "Example",
        "location": "Remote",
        "description": "Original description",
    }
    created = client.post("/jobs", json=payload)
    assert created.status_code == 201
    job_id = created.json()["id"]

    updated_payload = {**payload, "description": "Updated description"}
    updated = client.put(f"/jobs/{job_id}", json=updated_payload)
    assert updated.status_code == 200
    assert updated.json() == {"id": job_id, **updated_payload}
    assert client.get(f"/jobs/{job_id}").json() == updated.json()

    replacement = {key: value for key, value in payload.items() if key != "description"}
    cleared = client.put(f"/jobs/{job_id}", json=replacement)
    assert cleared.status_code == 200
    assert cleared.json() == {"id": job_id, **replacement, "description": None}
    assert client.get(f"/jobs/{job_id}").json() == cleared.json()


def test_create_job_rejects_non_text_description(client: TestClient):
    response = client.post("/jobs", json={
        "title": "Python Developer",
        "company": "Example",
        "location": "Remote",
        "description": 123,
    })

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "description"]
    assert client.get("/jobs").json() == []

def test_patch_job_updates_only_title(client: TestClient):
    payload = {
        "title": "Python Intern",
        "company": "WorkWise",
        "location": "Remote",
        "description": "Build backend APIs",
    }

    created = client.post("/jobs", json=payload)
    assert created.status_code == 201
    job_id = created.json()["id"]

    response = client.patch(
        f"/jobs/{job_id}",
        json={"title": "Python Developer"},
    )

    expected = {
        "id": job_id,
        **payload,
        "title": "Python Developer",
    }

    assert response.status_code == 200
    assert response.json() == expected
    assert client.get(f"/jobs/{job_id}").json() == expected


def test_patch_description_can_be_replaced_and_cleared(client: TestClient):
    payload = {
        "title": "Python Intern",
        "company": "WorkWise",
        "location": "Remote",
        "description": "Original description",
    }
    created = client.post("/jobs", json=payload)
    assert created.status_code == 201
    job_id = created.json()["id"]

    for description in ("Updated description", None):
        response = client.patch(
            f"/jobs/{job_id}", json={"description": description}
        )
        expected = {"id": job_id, **payload, "description": description}
        assert response.status_code == 200
        assert response.json() == expected
        assert client.get(f"/jobs/{job_id}").json() == expected


@pytest.mark.parametrize("changes", [
    {"title": None},
    {"company": None},
    {"location": None},
    {"title": ""},
    {"description": 123},
    {"titel": "Misspelled field"},
])
def test_patch_rejects_invalid_changes_without_modifying_job(
    client: TestClient, changes: dict
):
    payload = {
        "title": "Python Intern",
        "company": "WorkWise",
        "location": "Remote",
        "description": "Keep this description",
    }
    created = client.post("/jobs", json=payload)
    assert created.status_code == 201
    job_id = created.json()["id"]

    response = client.patch(f"/jobs/{job_id}", json=changes)

    assert response.status_code == 422
    assert client.get(f"/jobs/{job_id}").json() == created.json()


def test_patch_missing_job_returns_404(client: TestClient):
    response = client.patch("/jobs/999", json={"title": "Python Developer"})

    assert response.status_code == 404
    assert response.json() == {"detail": "Job not found"}
    assert client.get("/jobs").json() == []


def test_empty_patch_leaves_job_unchanged(client: TestClient):
    created = client.post("/jobs", json={
        "title": "Python Intern",
        "company": "WorkWise",
        "location": "Remote",
        "description": "Keep this description",
    })
    assert created.status_code == 201
    job_id = created.json()["id"]

    response = client.patch(f"/jobs/{job_id}", json={})

    assert response.status_code == 200
    assert response.json() == created.json()
    assert client.get(f"/jobs/{job_id}").json() == created.json()
