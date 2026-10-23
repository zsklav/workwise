# WorkWise: understand it, explain it, then deploy it

Study in this order. For each section, trace one request in the code, run the
example, and explain why each transaction/permission/acknowledgement exists.
Deployment comes after this walkthrough and the operational decisions it exposes.

## Your 60-second introduction

"WorkWise is a FastAPI backend for companies, jobs and job applications. PostgreSQL
holds authoritative business state. Owners manage their companies/jobs and review
applications, while applicants access their own records. Version checks protect
concurrent edits, and client idempotency keys make creation retries safe. Business
changes and notification events commit together through a transactional outbox.
Publishers send events to RabbitMQ; workers save deduplicated notifications in a
separate database. Docker CI tests permissions, concurrent requests, failure
recovery, multiple API/worker instances and backup restores. Logs, metrics and
distributed traces make the flow observable."

Explain each sentence with a concrete request and failure scenario rather than
memorizing the paragraph.

## 1. Follow an ordinary HTTP request

Read `workwise/api/main.py`, `schemas.py`, `dependencies.py`, `routes/jobs.py`,
then `workwise/db/session.py`. Start with `GET /jobs`, then `POST /jobs`.

- FastAPI matches the route and validates path/query/body input through Pydantic.
- Dependencies provide the authenticated user when needed and one request's session.
- SQLAlchemy generates parameterized SQL; PostgreSQL stores and constrains rows.
- The route commits a complete mutation and returns a typed response.

Practice: a schema validates data; a session tracks a transaction; a table stores
data. A session is request-scoped, while the engine/pool can be shared.

## 2. Explain the data model

Read `workwise/db/models.py` and migration `0004`, then `0011`.

Users own companies. Jobs reference companies. Applications reference jobs and
applicants. Foreign keys preserve those links; unique constraints enforce company
names and one application per applicant/job. Application history prevents deleting
its job, and existing applications prevent moving a job to another company.

Practice: explain why an API-only "check then insert" is insufficient when two
requests arrive together, and why a database constraint is still necessary.

## 3. Authentication and authorization

Read `workwise/core/security.py`, `auth_sessions.py`, `auth_limits.py` and
`workwise/api/routes/auth.py`.

Argon2 stores password hashes. Access JWTs carry signed identity, expiry and a
session version. Each authenticated request checks the current active user/version.
Opaque refresh tokens are hashed in the database, rotate on use and retain an
absolute expiry. Logout/reuse containment revokes the account's sessions. Shared
PostgreSQL rate windows limit authentication attempts across API instances.

Practice: authentication identifies the caller; authorization decides whether
that caller may manage this company or view this application. Explain why signing
a JWT does not grant ownership of every resource.

## 4. Concurrent updates

Read `workwise/api/concurrency.py` and `tests/api/test_concurrency.py`.

Two clients read version 1. One update uses `WHERE id = ... AND version = 1` and
advances to 2. A stale update affects no row and becomes `409`. The caller fetches
the current resource, reviews it and retries with its new version. Application
reviews use the same database predicate, including when two employers review at once.

Practice: explain the difference between an early version comparison in Python
and the final atomic comparison in PostgreSQL.

## 5. Safe creation retries

Read `workwise/api/idempotency.py` and [its API contract](idempotency-applications.md).

Suppose a job commits but its HTTP response is lost. Retrying with the same key
returns its original creation response. A unique actor/operation/key reservation,
request hash, response snapshot, business row and outbox event share one transaction.
Different content with the same unexpired key returns `409`; keys expire after
24 hours. Identical unkeyed POSTs express separate creation intents.

Practice: idempotency handles repeated intent; optimistic concurrency handles
competing edits. Consumer deduplication handles repeated deliveries. Explain the
different identifiers and transaction boundaries for all three.

## 6. The transactional outbox

Read job/application creation and `workwise/messaging/publisher.py`.

Writing the business row and outbox event in one PostgreSQL transaction avoids a
gap between database success and event creation. A broker outage leaves a durable
pending event. Publishers claim rows with `FOR UPDATE SKIP LOCKED`, publish with
confirmation, and mark them published only afterward.

Practice: describe the crash after broker confirmation but before database commit.
The event remains pending and can be published again, which leads to the next section.

## 7. Delivery, acknowledgements and duplicates

Read `workwise/messaging/worker.py`, `events.py`, `broker.py`, `replay.py`, then
`workwise/notifications/store.py`.

The worker commits its receipt and inbox item together before ACK. Repeated event
IDs are recognized by the receipt primary key. Transient failures use bounded
retries; exhausted/invalid events go to a dead-letter queue for operator inspection
and replay. Publishing/consuming/replay use separate broker permissions.

Practice: describe crashes immediately before and after receipt commit. Explain
at-least-once transport with idempotent database effects, and why receipts survive
ordinary inbox retention.

## 8. Separate notification storage

Read `workwise/api/routes/notifications.py`, `workwise/notifications/api.py` and
`contracts.py`.

The public API authenticates the user, then calls the inbox service using a short
internal service token scoped to that subject. The inbox service queries only
that recipient. Notification storage is independent of the jobs database. Business
writes can succeed during an inbox-service outage; inbox reads temporarily return
503 and asynchronous delivery resumes when dependencies recover.

Practice: identify the strong transaction boundary inside each database and the
eventually consistent boundary between business changes and the user's inbox.

## 9. Explain a real business workflow

Trace `POST /jobs/{id}/applications` through owner review to the applicant's inbox.
Use separate applicant/owner tokens. Try a stale version, self-review, duplicate
application, private application lookup and an invalid transition.

Application state is authoritative in the jobs database. Events carry application
versions, but independent workers can deliver notifications out of order. Each
inbox row is a notification; it does not overwrite the application's current state.

Practice: explain why a rejected application cannot immediately become hired, and
which database check prevents competing requests from creating two applications.

## 10. Multiple instances and failure recovery

Run the [multiple-instance demo](multiple-instances.md) on the disposable stack.
Observe the gateway's backend header, two queue consumers and the stop/recovery.

APIs share database state and signing configuration. Publishers divide claimed
rows using database locks. Workers share durable receipt deduplication. HAProxy
checks readiness and directs new requests to the surviving API. An in-flight
request can still fail, so the client keeps its idempotency key for a retry.

Practice: process redundancy is demonstrated on one host. Identify the gateway,
broker, databases and host as remaining shared failure points before discussing
production availability.

## 11. Observability, performance and recovery

Use [monitoring](monitoring-retention.md), [performance evidence](performance.md),
[tracing](notifications-tracing.md) and [backup/restore](backup-restore.md).

Logs connect a request to its result without logging tokens/letters. Traces show
cross-process work. Metrics show error/latency/backlog trends; alerts need sustained
conditions. Query-plan experiments explain the measured index improvement and
deep-offset cost. Backups are tested by restoring and comparing data/schema/grants.

Practice: choose evidence for "requests are slow", "notifications are delayed", and
"can we recover data". Explain why a successful backup command alone does not prove
a recoverable system, and why two database snapshots need broker reconciliation.

## 12. Tests, releases and deployment preparation

Read [testing](testing.md), [Docker/CI](docker-ci.md) and [migrations](migrations.md).
Tests use the separate `workwise_test` database and isolated transactions. True
concurrency tests use independent committed sessions. CI also starts real containers,
interrupts services and verifies recovery. Schema migrations use an owner role;
runtime roles have narrower data permissions. New event consumers roll out before
new event producers in a staged deployment.

Before deployment, decide hosting/budget, HTTPS and ingress trust, secret storage,
volume durability, automated encrypted off-host backups, restore/reconciliation
procedures and external alert delivery. Native/Docker verification is complete
engineering evidence; live operational behavior is verified after an actual rollout.

## Practice without the code open

Explain these scenarios in your own words, including what commits, retries or fails:

1. A POST succeeds but its response times out.
2. Two API instances receive the same key simultaneously.
3. Two reviewers change one application from the same version.
4. RabbitMQ is unavailable during job creation.
5. A publisher crashes after confirmation.
6. A worker commits but crashes before ACK.
7. One API/publisher/worker process stops while replicas are running.
8. A database backup predates a logout or broker delivery.

Once you can draw these flows and explain their limits, use the same scenarios as
the deployment acceptance checks.
