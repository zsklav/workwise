"""Disposable multiple-instance demo: retries, process failure and application delivery."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import secrets
import subprocess
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

COMPOSE = ["docker", "compose", "--env-file", ".env.docker", "--profile", "scale"]
EXTRA = ("api-replica", "publisher-replica", "worker-replica", "gateway")
PRIMARY = ("api", "publisher", "notification-worker")


def compose(*args):
    return subprocess.check_output([*COMPOSE, *args], text=True, stderr=subprocess.STDOUT, timeout=180).strip()


def request(method, path, *, token=None, payload=None, key=None, port=8002, expected=200, form=False):
    headers = {"Authorization": "Bearer " + token} if token else {}
    if key is not None:
        headers["Idempotency-Key"] = key
    body = None
    if payload is not None:
        body = (urlencode(payload) if form else json.dumps(payload)).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
    try:
        result = urlopen(Request(f"http://127.0.0.1:{port}" + path, data=body, headers=headers, method=method), timeout=15)
    except HTTPError as error:
        result = error
    with result:
        assert result.status == expected, f"{method} {path}: expected {expected}, got {result.status}"
        data = result.read()
        return json.loads(data) if data else None, result.headers


def wait(check, label, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                print("Verified: " + label, flush=True)
                return value
        except (OSError, AssertionError, subprocess.CalledProcessError):
            pass
        time.sleep(.5)
    raise AssertionError("Timed out: " + label)


def signup(prefix):
    username, password = prefix + uuid4().hex, secrets.token_urlsafe(32)
    request("POST", "/auth/signup", payload={"username": username, "password": password}, expected=201, port=8001)
    return request("POST", "/auth/login", payload={"grant_type": "password", "username": username, "password": password}, form=True, port=8001)[0]["access_token"]


def sql(service, database, statement):
    return compose("exec", "-T", service, "psql", "-XAt", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", database, "-c", statement)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-service-interruption", action="store_true", required=True)
    parser.parse_args()
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "90", "--no-deps", *EXTRA)
        owner, applicant = signup("scale_owner_"), signup("scale_cand_")
        company = request("POST", "/companies", token=owner, payload={"name": "Scale demo " + uuid4().hex}, expected=201)[0]
        payload = {"title": "Multiple-instance job", "company_id": company["id"], "location": "Remote"}
        key = "scale_" + uuid4().hex
        def create(_):
            return request("POST", "/jobs", token=owner, payload=payload, key=key, expected=201)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(create, range(16)))
        job = results[0][0]
        assert all(body == job for body, _ in results)
        assert {headers["X-WorkWise-Backend"] for _, headers in results} == {"api-a", "api-b"}
        assert sum(headers["Idempotency-Replayed"] == "false" for _, headers in results) == 1
        assert sql("db", "workwise", f"SELECT count(*) FROM jobs WHERE company_id={company['id']}") == "1"
        assert sql("db", "workwise", f"SELECT count(*) FROM outbox_events WHERE payload->>'job_id'='{job['id']}'") == "1"
        print("Verified: concurrent retries across two API instances create one job and one outbox event", flush=True)

        compose("run", "--rm", "--no-deps", "broker-setup", "python", "-c",
                "import httpx; from workwise.messaging.broker import BrokerSettings,QUEUE; "
                "s=BrokerSettings(rabbitmq_user='workwise'); "
                "r=httpx.get('http://rabbitmq:15672/api/queues/%2F/'+QUEUE,auth=(s.rabbitmq_user,s.rabbitmq_password.get_secret_value()),trust_env=False); "
                "r.raise_for_status(); assert r.json()['consumers']>=2")
        print("Verified: two workers consume the shared queue", flush=True)
        compose("stop", *PRIMARY)
        wait(lambda: all(request("GET", "/health")[1].get("X-WorkWise-Backend") == "api-b" for _ in range(4)),
             "gateway removes stopped primary API and routes to the surviving replica")
        survivor = request("POST", "/jobs", token=owner, payload={**payload, "title": "Created after primary failure"},
                           key="survivor_" + uuid4().hex, expected=201)[0]
        application = request("POST", f"/jobs/{survivor['id']}/applications", token=applicant,
                              payload={"cover_letter": "Private scale demo letter"}, key="apply_" + uuid4().hex, expected=201)[0]
        path = f"/applications/{application['id']}"
        assert request("PATCH", path, token=applicant, payload={"version": 1, "status": "hired"}, expected=403)
        shortlisted = request("PATCH", path, token=owner, payload={"version": 1, "status": "shortlisted"})[0]
        hired = request("PATCH", path, token=owner, payload={"version": shortlisted["version"], "status": "hired"})[0]
        assert hired["version"] == 3
        request("PATCH", path, token=owner, payload={"version": 1, "status": "rejected"}, expected=409)
        def inbox_complete():
            data = request("GET", "/notifications", token=applicant)[0]
            rows = [row for row in data if row["job_id"] == survivor["id"]]
            return len(rows) == 3 and {row["message"].rsplit(": ", 1)[-1] for row in rows} == {"applied.", "shortlisted.", "hired."}
        wait(inbox_complete, "surviving publisher/worker deliver all application transitions to applicant inbox")
        assert request("GET", "/applications/mine", token=applicant)[0] == [hired]
        assert request("GET", f"/jobs/{survivor['id']}/applications", token=owner)[0] == [hired]

        # Re-publish existing event IDs: a new consumer must retain the durable deduplication boundary.
        event_payload = json.loads(sql("db", "workwise", f"SELECT payload FROM outbox_events WHERE payload->>'application_id'='{application['id']}' AND payload->>'status'='hired'"))
        code = ("import json; from workwise.messaging.broker import *; "
                "c=connect(BrokerSettings()); ch=prepare(c); "
                f"event=json.loads({json.dumps(json.dumps(event_payload))}); "
                "[publish(ch,QUEUE,json.dumps(event).encode(),message_id=event['event_id']) for _ in range(4)]; c.close()")
        compose("exec", "-T", "publisher-replica", "python", "-c", code)
        wait(lambda: sql("notification-db", "notifications", f"SELECT count(*) FROM notification_receipts WHERE event_id='{event_payload['event_id']}'") == "1",
             "re-delivery preserves one receipt")
        # A marker event after duplicates is consumed in FIFO order with one surviving worker.
        last = request("POST", "/jobs", token=owner, payload={**payload, "title": "Drain marker"}, expected=201)[0]
        wait(lambda: any(row["job_id"] == last["id"] for row in request("GET", "/notifications", token=owner)[0]),
             "surviving worker drains duplicate deliveries and reaches the marker event")
        assert inbox_complete()
        print("Multiple-instance and applications smoke test passed.", flush=True)
    finally:
        # Stop extra writers first so the later backup drill can pause all writers it knows.
        compose("stop", *EXTRA)
        compose("start", *PRIMARY)


if __name__ == "__main__":
    main()
