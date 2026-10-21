"""Stop/start Docker services to verify reliability. Run only on a disposable demo/CI stack."""
import argparse
import json
import secrets
import subprocess
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

COMPOSE = ["docker", "compose", "--env-file", ".env.docker"]


def compose(*args):
    return subprocess.check_output([*COMPOSE, *args], text=True, stderr=subprocess.STDOUT).strip()


def sql(service, database, statement):
    return compose("exec", "-T", service, "psql", "-U", "postgres", "-d", database, "-At", "-c", statement)


def wait_for(check, description, timeout=100):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if check():
                print("Verified: " + description, flush=True)
                return
        except (subprocess.CalledProcessError, OSError):
            pass
        time.sleep(1)
    raise AssertionError("Timed out: " + description)


def publisher_code(code):
    return compose("exec", "-T", "publisher", "python", "-c", code)


def send(body, event_id):
    publisher_code("from messaging.broker import *; c=connect(BrokerSettings()); ch=prepare(c); "
                   f"publish(ch, QUEUE, {body!r}, message_id={event_id!r}); c.close()")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-service-interruption", action="store_true", required=True,
                        help="Acknowledge this script stops the broker, worker and notification database")
    parser.parse_args()
    token = None

    def request(path, payload, form=False):
        headers = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        data = (urlencode(payload) if form else json.dumps(payload)).encode()
        with urlopen(Request("http://127.0.0.1:8001" + path, data=data, headers=headers), timeout=10) as response:
            return json.load(response)

    username, password = "distributed_" + uuid4().hex, secrets.token_urlsafe(32)
    request("/auth/signup", {"username": username, "password": password})
    token = request("/auth/login", {"grant_type": "password", "username": username, "password": password}, True)["access_token"]
    company = request("/companies", {"name": username})

    def new_event():
        job = request("/jobs", {"title": "Distributed failure demo", "company_id": company["id"], "location": "Remote"})
        payload = sql("db", "workwise", f"SELECT payload FROM outbox_events WHERE payload->>'job_id' = '{job['id']}'")
        return json.loads(payload)

    def receipt_count(event_id):
        return sql("notification-db", "notifications", f"SELECT count(*) FROM notification_receipts WHERE event_id = '{event_id}'")

    def published(event_id):
        return sql("db", "workwise", f"SELECT published_at IS NOT NULL FROM outbox_events WHERE id = '{event_id}'") == "t"

    try:
        compose("stop", "notification-worker")
        first = new_event()
        first_id = first["event_id"]
        wait_for(lambda: published(first_id), "event queued while worker is stopped")
        assert receipt_count(first_id) == "0"
        compose("start", "notification-worker")
        wait_for(lambda: receipt_count(first_id) == "1", "worker restart drains queued event")

        compose("stop", "rabbitmq")
        second = new_event()
        assert not published(second["event_id"])
        print("Verified: job succeeds during broker outage; event stays pending", flush=True)
        compose("start", "rabbitmq")
        wait_for(lambda: receipt_count(second["event_id"]) == "1", "publisher reconnects and drains outbox after broker restart")

        send(json.dumps(first).encode(), first_id)
        wait_for(lambda: any('"notification_duplicate"' in line and first_id in line
                             for line in compose("logs", "--no-color", "notification-worker").splitlines()),
                 "duplicate delivery handled")
        assert receipt_count(first_id) == "1"

        compose("stop", "notification-db")
        third = new_event()
        third_id = third["event_id"]
        wait_for(lambda: any('"notification_dead_lettered"' in line and third_id in line
                             for line in compose("logs", "--no-color", "notification-worker").splitlines()),
                 "database outage exhausts bounded retries and reaches DLQ", timeout=120)
        compose("start", "notification-db")
        wait_for(lambda: receipt_count(third_id) == "0", "notification database recovers without partial record")
        compose("exec", "-T", "publisher", "python", "-m", "messaging.replay", "--event-id", third_id)
        wait_for(lambda: receipt_count(third_id) == "1", "operator replay records recovered event")

        send(b"invalid-json", str(uuid4()))
        wait_for(lambda: '"reason": "invalid_event"' in compose("logs", "--no-color", "notification-worker"),
                 "poison message quarantined")
        publisher_code("from messaging.broker import *; c=connect(BrokerSettings()); ch=prepare(c); "
                       "m,p,b=ch.basic_get(DLQ, auto_ack=False); assert m and b == b'invalid-json'; "
                       "assert p.headers['failure_type'] == 'invalid_event'; c.close()")
        compose("exec", "-T", "notification-worker", "python", "-c",
                "import os; assert all(k not in os.environ for k in ('DB_PASSWORD','JWT_SECRET','MIGRATION_DB_PASSWORD','NOTIFICATION_ADMIN_PASSWORD'))")
        compose("exec", "-T", "notification-worker", "python", "-c",
                "from messaging.notification_store import *; e=store_engine(NotificationSettings()); "
                "c=e.connect(); assert c.scalar(text('SELECT version FROM notification_schema_version')) == 1; "
                "assert c.scalar(text(\"SELECT has_table_privilege(current_user, 'notification_receipts', 'INSERT')\")); "
                "assert not c.scalar(text(\"SELECT has_table_privilege(current_user, 'notification_receipts', 'DELETE')\")); "
                "assert not c.scalar(text(\"SELECT has_schema_privilege(current_user, 'public', 'CREATE')\")); c.close(); e.dispose()")
        print("Distributed smoke test passed: outages, restart, deduplication, retries, DLQ, replay, credential separation.", flush=True)
    finally:
        # Restore services even when an assertion fails; volumes and demo rows are retained.
        compose("start", "rabbitmq", "notification-db", "notification-worker")


if __name__ == "__main__":
    main()
