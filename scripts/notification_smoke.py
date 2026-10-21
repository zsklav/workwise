"""Verify real inbox delivery, ownership and exported traces on a disposable Docker stack."""
import argparse
import json
import secrets
import subprocess
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

COMPOSE = ["docker", "compose", "--env-file", ".env.docker"]


def compose(*args):
    return subprocess.check_output([*COMPOSE, *args], text=True, stderr=subprocess.STDOUT).strip()


def request(method, path, *, token=None, payload=None, expected=200, form=False):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = None
    if payload is not None:
        body = (urlencode(payload) if form else json.dumps(payload)).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
    try:
        result = urlopen(Request("http://127.0.0.1:8001" + path, data=body, headers=headers, method=method), timeout=10)
    except HTTPError as error:
        result = error
    with result:
        assert result.status == expected, f"{method} {path}: expected {expected}, received {result.status}"
        data = result.read()
        return json.loads(data) if data else None, result.headers


def wait_for(check, label, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                print("Verified: " + label, flush=True)
                return value
        except (OSError, subprocess.CalledProcessError, AssertionError):
            pass
        time.sleep(0.5)
    raise AssertionError("Timed out: " + label)


def signup():
    username, password = "inbox_" + uuid4().hex, secrets.token_urlsafe(32)
    request("POST", "/auth/signup", payload={"username": username, "password": password}, expected=201)
    data, _ = request("POST", "/auth/login", payload={"grant_type": "password", "username": username, "password": password}, form=True)
    return data["access_token"], username


def trace_services(identity):
    with urlopen("http://127.0.0.1:16686/api/traces/" + identity, timeout=5) as result:
        data = json.load(result)
    return {process["serviceName"] for item in data.get("data", []) for process in item.get("processes", {}).values()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-service-interruption", action="store_true", required=True)
    parser.parse_args()
    token, username = signup()
    other_token, _ = signup()
    request("GET", "/notifications", expected=401)
    company, _ = request("POST", "/companies", token=token, payload={"name": username}, expected=201)

    def create():
        return request("POST", "/jobs", token=token, expected=201,
                       payload={"title": "Inbox smoke job", "company_id": company["id"], "location": "Remote"})

    def inbox_for(job_id):
        data, _ = request("GET", "/notifications", token=token)
        return next((item for item in data if item["job_id"] == job_id), None)

    try:
        job, headers = create()
        identity = headers["X-Trace-ID"]
        notification = wait_for(lambda: inbox_for(job["id"]), "job event delivers to creator inbox")
        assert notification["read_at"] is None
        data, headers = request("GET", "/notifications?unread_only=true", token=token)
        inbox_trace = headers["X-Trace-ID"]
        assert notification in data
        data, _ = request("GET", "/notifications", token=other_token)
        assert data == []
        path = f"/notifications/{notification['event_id']}/read"
        request("PATCH", path, token=other_token, expected=404)
        marked, _ = request("PATCH", path, token=token)
        assert marked["read_at"]
        time.sleep(0.1)
        repeated, _ = request("PATCH", path, token=token)
        assert repeated["read_at"] == marked["read_at"]
        unread, _ = request("GET", "/notifications?unread_only=true", token=token)
        assert unread == []
        print("Verified: recipient isolation, unread filter and idempotent read marker", flush=True)

        wait_for(lambda: {"workwise-api", "workwise-publisher", "workwise-notification-worker"} <= trace_services(identity),
                 "API -> durable outbox -> publisher -> worker spans exported to one Jaeger trace")
        wait_for(lambda: {"workwise-api", "workwise-notification-api"} <= trace_services(inbox_trace),
                 "authenticated HTTP gateway and notification service share a trace")

        compose("stop", "notification-api")
        request("GET", "/notifications", token=token, expected=503)
        offline_job, _ = create()
        compose("start", "notification-api")
        wait_for(lambda: inbox_for(offline_job["id"]), "notification API outage preserves job writes and delivered inbox state")

        compose("stop", "jaeger")
        collector_offline_job, _ = create()
        wait_for(lambda: inbox_for(collector_offline_job["id"]), "trace collector outage does not block job or notification delivery")
        compose("start", "jaeger")
        compose("exec", "-T", "notification-api", "python", "-c",
                "import os; assert all(k not in os.environ for k in ('JWT_SECRET','DB_PASSWORD','NOTIFICATION_DB_PASSWORD','NOTIFICATION_ADMIN_PASSWORD'))")
        print("Inbox and tracing smoke test passed.", flush=True)
    finally:
        compose("start", "notification-api", "jaeger")


if __name__ == "__main__":
    main()
