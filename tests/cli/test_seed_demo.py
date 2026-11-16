from scripts.deploy.seed_demo import COMPANIES, seed


def test_seed_creates_demo_data_once_and_reuses_it(db_client):
    expected_jobs = sum(len(jobs) for jobs in COMPANIES.values())
    assert seed(db_client) == {"companies": len(COMPANIES), "jobs": expected_jobs}
    assert seed(db_client) == {"companies": 0, "jobs": 0}

    listed = db_client.get("/v1/jobs", params={"limit": 100})
    assert listed.headers["X-Total-Count"] == str(expected_jobs)
    assert db_client.get("/v1/jobs", params={"remote": "true"}).headers["X-Total-Count"] == "4"
