# Monitoring and retention

The optional Compose `monitoring` profile runs Prometheus and Grafana alongside
the existing services. It adds durable aggregate metric storage, a provisioned
dashboard, and seven evaluated alert rules. Alerts are visible locally; there
is no external email/Slack/paging receiver configured.

## Start and inspect

From the repository root with Docker available:

```powershell
python -m scripts.docker.create_env --add-missing
docker compose --env-file .env.docker --profile monitoring up --build --detach --wait
```

This preserves existing secrets and adds random `METRICS_TOKEN` and
`GRAFANA_ADMIN_PASSWORD` values to ignored `.env.docker`. No values are printed.
The native Windows host currently has no Docker installation; actual container
verification runs on GitHub's Ubuntu CI runner.

- Grafana: <http://127.0.0.1:3000>, username `workwise_admin`, password from your
  local `.env.docker`. Open **WorkWise / WorkWise overview**.
- Prometheus: <http://127.0.0.1:9090>, inspect **Targets**, **Alerts**, and query data.
- Jaeger remains available at <http://127.0.0.1:16686>.

The 14 panels show scrape/database availability, HTTP traffic/errors/p95,
pending/published outbox counts and oldest pending age, queue waiting/unacked
messages and consumers, unread/read inbox counts, and firing alerts. HTTP
latency/traffic panels include operational probes and scrapes; they are service
observations, not application-only benchmarks. Low traffic can produce gaps in
rate/quantile panels. Queue metrics contain queue names, not message payloads.

Prometheus scrapes every 10 seconds and retains data for at most seven days or
512 MB of retained blocks, whichever threshold is reached first. WAL/head data
can exceed that size target, so this is not a strict disk quota. Grafana and
Prometheus use persistent named volumes. Normal `docker compose down` preserves
them; `down --volumes` deletes them along with application volumes and belongs
only on disposable stacks.

Prometheus has no login in this local setup; its port is bound to loopback.
Grafana requires its separate random admin password and disables signup and
anonymous access. Do not publish either directly to the internet. Existing
Grafana volumes keep their stored admin password; changing the startup variable
alone does not reset it.

## Scrape authentication and database failures

`/metrics` retains its existing user/admin JWT authorization. Automated scraping
instead uses `/internal/metrics` on each API with a dedicated bearer
`METRICS_TOKEN`, independent of user accounts and database authentication. The
endpoint is absent from OpenAPI, returns 404 when unconfigured and 401 for missing
or wrong credentials. Native `.env` does not acquire a token automatically.

The setup container writes the token to an isolated named volume; only that
initializer and Prometheus mount it. No secret is committed in the scrape YAML.
The initializer runs as root solely to populate its new Docker volume, drops
capabilities and has no database credentials. Prometheus reads the volume only.
The APIs receive only the scrape token, never Grafana's password. To rotate it,
update `.env.docker` privately and recreate the initializer and API containers
together, then verify Targets. No token is sent to RabbitMQ.

Each scrape includes existing in-process HTTP counters/histograms plus live
database aggregate gauges. A separate one-connection inspection pool has a
one-second pool wait, two-second connect/query limits and a one-second lock
timeout. A database/query failure still returns HTTP metrics with
`workwise_database_up 0`; unknown database aggregates are omitted, not exported
as zero. SQL exception details and credentials are not returned. Pool, DNS,
network and driver behavior can still add time; Prometheus imposes a five-second
scrape timeout. HTTP metrics remain per process and reset on restart. The stack
uses one Uvicorn process per API; multiple workers need a collection redesign.

RabbitMQ's built-in Prometheus plugin exposes per-object metrics internally on
15692. That port has no host binding. Per-queue labels are suitable for the small
demo topology; large dynamic queue fleets need a cardinality review. The plugin
configuration is mounted for both new and existing broker volumes.

| Alert | Condition and hold time |
|---|---|
| ScrapeTargetDown | API/inbox/broker scrape unavailable for 1 minute |
| DatabaseUnavailable | Aggregate database inspection fails for 1 minute |
| OutboxDelayed | Oldest unpublished event exceeds 120 seconds for 1 minute |
| NotificationQueueBacklog | Main queue has over 100 waiting messages for 5 minutes |
| NotificationConsumerMissing | Main queue has zero consumers for 1 minute |
| DeadLettersPresent | DLQ has waiting messages for 1 minute |
| HttpServerErrors | Over 5% 5xx and at least 10 observed requests in five minutes, sustained 2 minutes |

These are demo thresholds, not measured SLOs. The HTTP rule includes probes and
scrapes, so operational traffic can affect the denominator. Adjust using real
traffic measurements before using it for production decisions. If Prometheus
itself stops, this stack cannot alert about its own failure. There is no HA
monitoring, external log store, alert delivery, or automatic remediation.

## Safe maintenance batches

Retention is an explicitly invoked operator command, not a background task
inside the public API. Preview is the default; `--apply` is required to delete.
One invocation processes at most 500 rows by default (allowed 1..1000), and
retention windows must be 7..3650 days. Each batch is one transaction with a
five-second statement timeout and one-second lock timeout. Concurrent cleaners
use `FOR UPDATE SKIP LOCKED`, so they do not claim the same rows.

| Target | Default eligibility | Always preserved |
|---|---|---|
| outbox | `published_at` strictly older than 30 days | Unpublished events, recent publications, jobs/companies/users |
| inbox | Both `read_at` and `created_at` strictly older than 90 days | Unread/recent items and **all deduplication receipts** |

An old event published recently is retained. An old inbox item read recently is
retained. Rows exactly at the cutoff survive. Receipt IDs and payloads are kept
indefinitely so a delayed retry/DLQ replay cannot recreate a notification that
retention removed. Receipt storage growth remains a deliberate tradeoff until
a separate replay-window/tombstone design is implemented. Published outbox
cleanup removes historical audit/republication source rows, not queued messages
or DLQ content; choose the retention window to match your recovery needs.

Preview in Docker:

```powershell
docker compose --env-file .env.docker run --rm retention-outbox
docker compose --env-file .env.docker run --rm retention-inbox
```

Apply a single reviewed batch:

```powershell
docker compose --env-file .env.docker run --rm retention-outbox python -m workwise.operations.retention outbox --days 30 --batch-size 500 --apply
docker compose --env-file .env.docker run --rm retention-inbox python -m workwise.operations.retention inbox --days 90 --batch-size 500 --apply
```

For native jobs storage, `python -m workwise.operations.retention outbox` previews
using existing migration credentials. Inbox maintenance uses the isolated
notification database's operator credentials. The tools use privileged roles
only for these short-lived operator invocations; public runtime roles gain no
DELETE privileges. They are not enabled as continuously running services.
No automatic schedule is installed. Repeat batches deliberately or add an
operator-controlled scheduler later. A report's `selected` is capped at the
batch limit, not a count of every eligible row. Zero can also mean all eligible
rows are currently locked. Outbox/inbox cleanup are independent transactions.

Maintenance reports contain target, mode, cutoff, batch limit and counts;
they exclude credentials, payloads and row identifiers. Unexpected failures
roll back that batch. On large histories, aggregate scans and finding candidates
still need indexes/query measurement; batch limits do not bound scan cost.
No schema migration, native data deletion or account changes are required by
this checkpoint.

## Verification

The Python tests cover credential isolation, disabled endpoints, database-down
scrapes, policy validation, previews, cutoffs, transactional rollback, skipped
locks, and preserving unread items/receipts. A replay after inbox cleanup is
deduplicated without recreating the deleted item.

CI starts the monitoring profile, validates config with `promtool`, checks all
seven rules against failing and healthy synthetic time series, then runs:

```powershell
python -m scripts.smoke.monitoring --allow-service-interruption
```

This disposable-only demo checks all three scrape targets, both database
gauges, actual RabbitMQ queue metrics and authenticated Grafana provisioning.
It previews/applies synthetic old rows, verifies protected records survive,
and stops the worker long enough to observe a real firing consumer alert.
Restarting the worker must resolve it. Synthetic fixture rows are removed
before publisher/worker restart; personal/native data is not used.

References: [Prometheus scrape authorization](https://prometheus.io/docs/prometheus/latest/configuration/configuration/),
[Grafana provisioning](https://grafana.com/docs/grafana/latest/administration/provisioning/),
[RabbitMQ Prometheus plugin](https://www.rabbitmq.com/docs/prometheus),
[Prometheus storage retention](https://prometheus.io/docs/prometheus/latest/storage/).
