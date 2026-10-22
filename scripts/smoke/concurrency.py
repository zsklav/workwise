"""Race two HTTP writers against jobs/companies on an isolated disposable demo stack."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import secrets
from threading import Barrier
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    base = parser.parse_args().base_url.rstrip("/")
    token = None

    def request(method, path, payload=None, *, expected=200, form=False):
        headers = {"Authorization": "Bearer " + token} if token else {}
        body = None
        if payload is not None:
            body = (urlencode(payload) if form else json.dumps(payload)).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
        try:
            result = urlopen(Request(base + path, data=body, headers=headers, method=method), timeout=10)
        except HTTPError as error:
            result = error
        with result:
            if expected is not None:
                assert result.status == expected, f"{method} {path}: expected {expected}, received {result.status}"
            content = result.read()
            return result.status, json.loads(content) if content else None

    username, password = "concurrency_" + uuid4().hex, secrets.token_urlsafe(32)
    request("POST", "/auth/signup", {"username": username, "password": password}, expected=201)
    _, login = request("POST", "/auth/login", {"grant_type": "password", "username": username, "password": password}, form=True)
    token = login["access_token"]
    _, company = request("POST", "/companies", {"name": username}, expected=201)
    company_path = f"/companies/{company['id']}"
    _, job = request("POST", "/jobs", {"title": "Concurrent job", "company_id": company["id"], "location": "Remote"}, expected=201)
    job_path = f"/jobs/{job['id']}"
    try:
        for path, field, fields in (
            (job_path, "title", ("title", "company_id", "location", "description")),
            (company_path, "name", ("name", "description")),
        ):
            for method in ("PUT", "PATCH"):
                _, snapshot = request("GET", path)
                barrier = Barrier(2)
                def write(label):
                    payload = {key: snapshot[key] for key in fields} if method == "PUT" else {}
                    payload.update(version=snapshot["version"])
                    payload[field] = f"{username} {method} {label}"
                    barrier.wait(timeout=5)
                    return request(method, path, payload, expected=None)
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [executor.submit(write, label) for label in ("left", "right")]
                    results = [future.result(timeout=15) for future in futures]
                assert sorted(status for status, body in results) == [200, 409]
                winner = next(body for status, body in results if status == 200)
                assert winner["version"] == snapshot["version"] + 1
                assert request("GET", path)[1] == winner
                print(f"Verified: concurrent {method} {path.split('/')[1]} has one winner and one conflict", flush=True)
    finally:
        for path in (job_path, company_path):
            status, latest = request("GET", path, expected=None)
            if status == 200:
                request("DELETE", path + f"?version={latest['version']}", expected=204)
    print("Concurrent HTTP write smoke test passed.", flush=True)


if __name__ == "__main__":
    main()
