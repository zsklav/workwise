# Workload measurements and targeted query tuning

This phase adds repeatable HTTP workloads, query-plan evidence and one measured
index change. It establishes a development baseline, not a production capacity
claim or a measured HTTP speedup over the previous commit.

## What is measured

The HTTP runner seeds 20,000 synthetic jobs across 100 companies by default,
then starts a separate native Uvicorn process or calls the disposable Compose
API. Dataset setup and login are excluded from timing. Six scenarios run
separately, each at 1 and 8 concurrent clients, with two measured repeats:

1. Repeated retrieval of one job.
2. The first page of jobs.
3. First-page exact company-ID filtering.
4. First-page exact company-name filtering.
5. A deep offset at the end of the original fixture.
6. Authenticated job creation, including the atomic outbox write.

Scenarios run in this order, so writes do not change the read fixture. Writes
reuse one owned company and create distinct jobs from identical valid bodies.
The bulk fixture itself is loaded directly through SQL and emits no events;
only HTTP creates produce outbox events. Warmup creates are real writes and
are included in the expected event count, but excluded from measured rounds.

Each repeat has its own persistent HTTPX connection pool sized to the selected
concurrency. Ten warmup calls precede each measured round. This is a closed-loop
workload: each client starts its next request after its previous response.
There is no fixed arrival rate, think time, coordinated-omission correction,
soak test, sustained saturation target or distributed load generator.

JSON and Markdown reports record per scenario/client count/repeat:

- Attempted requests per second and successful requests per second.
- p50/p95/p99/max client latency using nearest-rank percentiles.
- Actual HTTP statuses, transport-error types, failure counts and failure rate.
- Fixture size, client Python/platform/CPU count, schema revision and configuration.

Latency covers the pooled HTTP request through reading the response body; JSON
validation follows that timer. Total round time includes scheduling and
validation overhead. Latency percentiles include failed attempts, so inspect
failure rates too. Validation checks status, response shape, ordering and the
requested company filter. Error bodies, bearer tokens, passwords and arbitrary
exception messages are excluded from reports. CPU count describes the client
host, not allocated server capacity. CI runner contention is not controlled.

Maximums are 32 concurrent clients, four distinct concurrency levels, five
repeats, 10,000 requests per round and 50,000 total calls including warmup.
HTTP read/write timeout is ten seconds, connect timeout three seconds. HTTPX
proxies and redirects are disabled. A timeout is reported as a failure; it is
not proof that a POST did not commit. These writes are not automatically retried.

## Run natively

With PostgreSQL running and dependencies installed:

```powershell
python -m pytest -q --tb=short
python -m scripts.performance.run --rows 20000 --requests 100 --clients 1 8 --repeats 2 --warmup 10
```

Tests prepare the dedicated `workwise_test` schema. The native runner always
selects that database, refuses pre-existing jobs/companies, seeds a uniquely
owned fixture, and starts a hidden temporary server on an available loopback
port. It never uses the development `workwise` API/rows. Do not run database
tests and this benchmark simultaneously. The native server has one worker,
disabled OTLP export and request logging to ignored `performance-results/native-server.log`.
Other `.env` behavior still applies; it is not an isolated CPU/network sandbox.

On normal completion or a handled failure, it stops the temporary server and
removes only its owned jobs/companies, recipient outbox rows and test account.
A killed process or power loss can leave synthetic rows; the next run refuses
to erase them. Inspect and deliberately remove the fixture before retrying.
The database identity sequences and planner statistics can advance during the
fixture; cleanup restores records, not physical database state or sequence values.

Reports are ignored files `performance-results/http.json` and `http.md`.
No publisher/notification worker is started natively; event count is checked,
but notification delivery is not measured in native mode.

## Run against the disposable Docker stack

The `benchmark` tool uses an independent client container and the existing
single-worker API, with its normal logging/tracing and background services.
It requires empty jobs/companies and explicit disposable-mode acknowledgement
in its default command. It uses short-lived operator credentials for fixture
setup/cleanup; the API gains no schema or user-deletion privileges.

```powershell
docker compose --env-file .env.docker up --build --detach --wait
docker compose --env-file .env.docker run --rm --no-deps benchmark
```

Do not run this against a personal stack containing jobs/companies. The default
Docker command measures 60 requests per round (1,440 measured calls total).
Override the container command to change settings:

```powershell
docker compose --env-file .env.docker run --rm --no-deps benchmark python -m scripts.performance.run --mode docker --allow-disposable-stack --rows 20000 --requests 100 --clients 1 8 --repeats 2
```

Reports persist in a dedicated Docker volume. Read them without exposing secrets:

```powershell
docker compose --env-file .env.docker run --rm --no-deps benchmark cat /app/performance-results/http.md
```

After measured writes, Docker mode checks that every create produced an outbox
event and waits up to 30 seconds of polling for the same number of distinct
items in that account's real inbox. The final drain duration is time **after
HTTP measurements finish**, not per-event delivery latency. Poll/query time can
extend the deadline by an in-flight request. Queue processing overlaps HTTP
writes; a short final drain does not imply every event had low latency.
Inbox/receipt history remains in the disposable notification database after
the benchmark's account/jobs are cleaned up. No external messages are sent.

CI runs this before failure-recovery demos, gates on zero request/validation
failures plus outbox/inbox correctness, and uploads sanitized JSON/Markdown as
`docker-http-measurement` for seven days. It does not gate on fixed latency or
throughput values on shared runners. The separate PostgreSQL job uploads the
SQL evidence as `query-plan-measurement`. Reports retain the CI commit SHA.

## SQL evidence and migration 0009

Run the controlled query experiment:

```powershell
python -m scripts.performance.query_plan
```

It uses only temporary tables in `workwise_test`: 50,000 jobs, 100 companies,
interleaved company IDs, 120-byte descriptions and seven repetitions per query.
It compares a single-column company index against `(company_id, id)` using
`EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON)`, warming each query first.
Temporary rows/indexes disappear at commit. Application tables are unchanged.

Native PostgreSQL 17.6 / Python 3.13.5 measurements on 2026-10-22:

| Query | Single-column median ms | Composite median ms | Buffer accesses before / after |
|---|---:|---:|---:|
| Company first page | 0.570 | 0.111 | 55 / 23 |
| Company deep offset | 1.418 | 1.261 | 503 / 504 |
| Unfiltered deep offset | 55.366 | 61.543 | 1501 / 1501 |

The first-page plan switched from scanning the primary-key index and filtering
jobs to scanning the company/order index. Its lower buffer work supports the
targeted change. The composite index was 1,138,688 bytes versus 540,672 bytes
for the single-column index in this fixture: it trades extra storage and write
maintenance for ordered company lookup. Timings include EXPLAIN overhead and
temporary-table caching; they are not end-to-end API latencies or disk-I/O counts.
Root buffer counters are inclusive, so child counters are not summed again.
Samples for different phases run sequentially, not in randomized A/B order.

Revision **0009** replaces `ix_jobs_company_id` with `ix_jobs_company_id_id`.
The leading company column still supports company lookup/FK checks while the
second column supplies the ID ordering. It preserves all row fields and
versions, adds no API parameters and changes no pagination semantics. Native
upgrade snapshots preserved all three development jobs and two companies.
Apply with `python -m scripts.database.migrate`. This transactional index build
can block writes and has a five-second lock timeout; large live tables need a
separately planned concurrent-index migration rather than this small-demo rollout.

Deep offsets still scan/discard preceding rows. The company deep-offset plan
still used bitmap scanning/sorting; the unfiltered case showed no reduction in
buffer work. There is no claim of a speedup for those cases. A future cursor
pagination contract is the next option to evaluate if that workload matters.

## Native HTTP checkpoint and limits

On Windows 11, Python 3.13.5, PostgreSQL 17.6, schema 0009, 20,000 fixture jobs
and 100 companies, the recorded native run completed **2,400 measured calls
with zero failures**, plus 240 warmup calls. All 440 warmup/measured creates
produced outbox events. Example ranges across the two repeats:

| Scenario | 1 client requests/s | 1 client p95 ms | 8 clients requests/s | 8 clients p95 ms |
|---|---:|---:|---:|---:|
| Job detail | 40.86–46.19 | 24.02–36.50 | 43.65–47.43 | 301.79–379.11 |
| Company-ID first page | 27.03–40.08 | 27.54–95.29 | 22.58–37.08 | 332.93–952.07 |
| Deep offset | 19.36–23.55 | 48.94–95.20 | 37.91–38.85 | 420.08–554.28 |
| Job create | 19.24–30.14 | 36.64–130.29 | 31.29–34.11 | 512.66–643.08 |

More concurrency did not consistently increase throughput and increased tail
latency. This run shares a Windows host, database, load generator, antivirus/
filesystem activity and file logging; it does not identify which resource
caused the variability. CPU/memory/disk saturation, pool-wait time, connection
limits and per-query attribution require separate profiling before changing
worker counts/pool settings or introducing caching. Short samples (100 requests
per repeat) give coarse p99 estimates. No production SLO or maximum throughput
is established. Docker results use a different environment and belong in their
own CI artifacts, not an apples-to-apples speedup claim.

References: [PostgreSQL multicolumn indexes](https://www.postgresql.org/docs/17/indexes-multicolumn.html),
[EXPLAIN](https://www.postgresql.org/docs/17/sql-explain.html),
[HTTPX pooling and limits](https://www.python-httpx.org/advanced/resource-limits/),
[HTTPX async clients](https://www.python-httpx.org/async/).
