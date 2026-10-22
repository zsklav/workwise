"""Verify monitoring/retention on a disposable stack; temporarily stops publisher/worker."""
import argparse
import base64
import json
from pathlib import Path
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


def get(url, headers=None):
    with urlopen(Request(url, headers=headers or {}), timeout=5) as response:
        return json.load(response)


def wait(check, label, timeout=100):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        try:
            if check():
                print("Verified: " + label, flush=True)
                return
        except (OSError, AssertionError, subprocess.CalledProcessError):
            pass
        time.sleep(1)
    raise AssertionError("Timed out: " + label)


def query(expression):
    result = get("http://127.0.0.1:9090/api/v1/query?" + urlencode({"query": expression}))
    assert result["status"] == "success"
    return result["data"]["result"]


def retention(service, target, apply=False):
    args = ["run", "--rm", "--no-deps", service, "python", "-m", "workwise.operations.retention", target]
    if apply:
        args.append("--apply")
    lines = compose(*args).splitlines()
    report = next(json.loads(line) for line in lines if line.startswith('{"event": "retention_batch"'))
    assert report["mode"] == ("apply" if apply else "preview")
    assert report["selected"] == 1 and report["deleted"] == int(apply)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-service-interruption", action="store_true", required=True)
    parser.parse_args()
    wait(lambda: len(query('up{job=~"workwise-api|workwise-inbox|rabbitmq"} == 1')) == 3,
         "Prometheus successfully scrapes both APIs and RabbitMQ")
    wait(lambda: len(query("workwise_database_up == 1")) == 2, "both database inspection gauges are healthy")
    wait(lambda: bool(query('rabbitmq_queue_consumers{queue="workwise.notifications.v1"} > 0')),
         "RabbitMQ exports per-queue consumer metrics")
    values = dict(line.split("=", 1) for line in Path(".env.docker").read_text().splitlines() if "=" in line and not line.startswith("#"))
    credentials = base64.b64encode(("workwise_admin:" + values["GRAFANA_ADMIN_PASSWORD"]).encode()).decode()
    dashboard = get("http://127.0.0.1:3000/api/dashboards/uid/workwise-overview", {"Authorization": "Basic " + credentials})
    assert len(dashboard["dashboard"]["panels"]) == 14
    print("Verified: authenticated Grafana has the provisioned WorkWise dashboard", flush=True)
    rules = get("http://127.0.0.1:9090/api/v1/rules")["data"]["groups"]
    assert sum(len(group["rules"]) for group in rules) == 7
    old, pending, read, unread = [str(uuid4()) for _ in range(4)]
    try:
        compose("stop", "publisher", "notification-worker")
        sql("db", "workwise", f"""INSERT INTO outbox_events(id,payload,created_at,published_at) VALUES
            ('{old}','{{}}',now()-interval '100 days',now()-interval '100 days'),
            ('{pending}','{{}}',now()-interval '100 days',NULL)""")
        sql("notification-db", "notifications", f"""INSERT INTO notification_receipts(event_id,payload) VALUES ('{read}','{{}}'),('{unread}','{{}}');
            INSERT INTO notifications(event_id,recipient_id,job_id,company_id,message,occurred_at,created_at,read_at) VALUES
            ('{read}',1,1,1,'Retention fixture',now()-interval '150 days',now()-interval '150 days',now()-interval '100 days'),
            ('{unread}',1,1,1,'Retention fixture',now()-interval '150 days',now()-interval '150 days',NULL)""")
        retention("retention-outbox", "outbox")
        assert sql("db", "workwise", f"SELECT count(*) FROM outbox_events WHERE id='{old}'") == "1"
        retention("retention-outbox", "outbox", apply=True)
        assert sql("db", "workwise", f"SELECT count(*) FROM outbox_events WHERE id='{pending}' AND published_at IS NULL") == "1"
        retention("retention-inbox", "inbox")
        retention("retention-inbox", "inbox", apply=True)
        assert sql("notification-db", "notifications", f"SELECT count(*) FROM notification_receipts WHERE event_id IN ('{read}','{unread}')") == "2"
        assert sql("notification-db", "notifications", f"SELECT count(*) FROM notifications WHERE event_id='{unread}' AND read_at IS NULL") == "1"
        assert sql("notification-db", "notifications", f"SELECT count(*) FROM notifications WHERE event_id='{read}'") == "0"
        print("Verified: bounded cleanup previews, deletes eligible rows and preserves pending/unread/receipts", flush=True)
        wait(lambda: bool(query('ALERTS{alertname="NotificationConsumerMissing",alertstate="firing"}')),
             "stopping the worker fires the configured sustained consumer alert", timeout=110)
    finally:
        # Remove only this disposable demo's synthetic rows before resuming delivery.
        try:
            sql("db", "workwise", f"DELETE FROM outbox_events WHERE id IN ('{old}','{pending}')")
            sql("notification-db", "notifications", f"DELETE FROM notifications WHERE event_id IN ('{read}','{unread}'); DELETE FROM notification_receipts WHERE event_id IN ('{read}','{unread}')")
        finally:
            compose("start", "publisher", "notification-worker")
    wait(lambda: not query('ALERTS{alertname="NotificationConsumerMissing",alertstate="firing"}'),
         "consumer alert resolves after the worker restarts", timeout=60)
    print("Monitoring and retention smoke test passed.", flush=True)


if __name__ == "__main__":
    main()
