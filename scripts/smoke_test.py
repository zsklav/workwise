"""Exercise an isolated disposable CI stack through real HTTP requests."""

import argparse
import json
import secrets
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import UUID, uuid4


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    base_url = parser.parse_args().base_url.rstrip("/")
    token = None

    def request(method, path, expected, payload=None, form=False):
        headers = {}
        if token:
            headers["Authorization"] = "Bearer " + token
        data = None
        if payload is not None:
            data = (urlencode(payload) if form else json.dumps(payload)).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
        req = Request(base_url + path, data=data, headers=headers, method=method)
        try:
            response = urlopen(req, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            assert response.status == expected, f"{method} {path}: expected {expected}, got {response.status}"
            assert UUID(response.headers["X-Request-ID"]).version == 4
            content = response.read()
            return json.loads(content) if content else None

    assert request("GET", "/health", 200) == {"status": "OK"}
    assert request("GET", "/ready", 200) == {"status": "ready"}
    request("GET", "/metrics", 401)
    request("POST", "/companies", 401, {"name": "Anonymous must fail"})
    username = "smoke_" + uuid4().hex
    password = secrets.token_urlsafe(32)
    user = request("POST", "/auth/signup", 201, {"username": username, "password": password})
    assert user["role"] == "member"
    token = request("POST", "/auth/login", 200, {
        "grant_type": "password", "username": username, "password": password,
    }, form=True)["access_token"]
    assert request("GET", "/auth/me", 200)["id"] == user["id"]
    request("GET", "/metrics", 403)
    company = request("POST", "/companies", 201, {"name": username})
    company_path = f"/companies/{company['id']}"
    payload = {"title": "Container smoke test", "company_id": company["id"], "location": "Remote"}
    job = request("POST", "/jobs", 201, payload)
    job_path = f"/jobs/{job['id']}"
    assert request("GET", job_path, 200)["company_id"] == company["id"]
    request("PUT", job_path, 200, {**payload, "location": "Hybrid"})
    request("PATCH", job_path, 200, {"description": "Real HTTP request"})
    renamed = request("PATCH", company_path, 200, {"name": username + " renamed"})
    assert request("GET", job_path, 200)["company"] == renamed["name"]
    request("DELETE", company_path, 409)
    request("DELETE", job_path, 204)
    request("DELETE", company_path, 204)
    print("Container HTTP smoke test passed: readiness, auth, ownership, CRUD, request IDs, and protected metrics.")


if __name__ == "__main__":
    main()
