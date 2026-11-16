"""Fill a running WorkWise API with fictional demo companies and jobs, through the public API.

Safe to run more than once: existing demo companies and jobs (matched by name and title)
are reused, not duplicated. Nothing is deleted.

    python -m scripts.deploy.seed_demo --base https://workwise-murex.vercel.app --username vanshikha_admin
"""
import argparse
from getpass import getpass
import sys

import httpx

DEMO_NOTE = "Fictional company created as WorkWise demo data."

COMPANIES = {
    "Northwind Labs": [
        {"title": "Backend Engineer (Python)", "location": "Bengaluru", "remote": False, "job_type": "full_time",
         "salary_min": 1200000, "salary_max": 2000000,
         "description": "Build FastAPI services and PostgreSQL schemas for an internal analytics platform."},
        {"title": "Platform Engineer", "location": "Remote", "remote": True, "job_type": "full_time",
         "salary_min": 1800000, "salary_max": 2800000,
         "description": "Own CI pipelines, Docker images and observability for a dozen services."},
        {"title": "Backend Intern", "location": "Bengaluru", "remote": False, "job_type": "internship",
         "salary_min": 30000, "salary_max": 50000,
         "description": "Six-month internship writing tests and small API features."},
    ],
    "Bluepeak Health": [
        {"title": "Data Engineer", "location": "Pune", "remote": False, "job_type": "full_time",
         "salary_min": 1400000, "salary_max": 2200000,
         "description": "Design batch pipelines and keep reporting tables fast and correct."},
        {"title": "QA Automation Engineer", "location": "Remote", "remote": True, "job_type": "contract",
         "salary_min": 900000, "salary_max": 1300000,
         "description": "Grow API and end-to-end test suites for patient scheduling software."},
    ],
    "Orbit Retail": [
        {"title": "Full Stack Developer", "location": "Hyderabad", "remote": False, "job_type": "full_time",
         "salary_min": 1000000, "salary_max": 1800000,
         "description": "Ship features across a React storefront and a Python order API."},
        {"title": "Site Reliability Engineer", "location": "Remote", "remote": True, "job_type": "full_time",
         "salary_min": 2000000, "salary_max": 3200000,
         "description": "Run on-call, tune PostgreSQL and automate incident response."},
        {"title": "Part-time Technical Writer", "location": "Remote", "remote": True, "job_type": "part_time",
         "salary_min": 400000, "salary_max": 600000,
         "description": "Write API guides and keep OpenAPI descriptions accurate."},
    ],
}


def fail(response: httpx.Response, action: str):
    raise SystemExit(f"{action} failed: HTTP {response.status_code} {response.text[:200]}")


def find_company(client: httpx.Client, name: str) -> dict | None:
    offset = 0
    while True:
        page = client.get("/v1/companies", params={"limit": 100, "offset": offset})
        if page.status_code != 200:
            fail(page, "Listing companies")
        items = page.json()
        for company in items:
            if company["name"] == name:
                return company
        if len(items) < 100:
            return None
        offset += 100


def seed(client: httpx.Client) -> dict[str, int]:
    """Create missing demo data with an already authenticated client. Returns created counts."""
    created = {"companies": 0, "jobs": 0}
    for name, jobs in COMPANIES.items():
        company = find_company(client, name)
        if company is None:
            response = client.post("/v1/companies", json={"name": name, "description": DEMO_NOTE})
            if response.status_code != 201:
                fail(response, f"Creating company {name!r}")
            company = response.json()
            created["companies"] += 1
        existing = client.get("/v1/jobs", params={"company_id": company["id"], "limit": 100})
        if existing.status_code != 200:
            fail(existing, "Listing jobs")
        titles = {job["title"] for job in existing.json()}
        for job in jobs:
            if job["title"] in titles:
                continue
            response = client.post("/v1/jobs", json={**job, "company_id": company["id"]})
            if response.status_code != 201:
                fail(response, f"Creating job {job['title']!r}")
            created["jobs"] += 1
    return created


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True, help="API base URL, for example https://workwise-murex.vercel.app")
    parser.add_argument("--username", required=True)
    args = parser.parse_args()
    password = getpass(f"Password for {args.username}: ")
    with httpx.Client(base_url=args.base.rstrip("/"), timeout=30) as client:
        login = client.post("/v1/auth/login", data={"grant_type": "password", "username": args.username,
                                                    "password": password})
        if login.status_code != 200:
            fail(login, "Login")
        client.headers["Authorization"] = "Bearer " + login.json()["access_token"]
        created = seed(client)
        total = client.get("/v1/jobs", params={"limit": 1}).headers.get("X-Total-Count", "?")
    print(f"Created {created['companies']} companies and {created['jobs']} jobs. Jobs now listed: {total}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
